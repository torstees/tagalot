"""The ingest context themes write through (DESIGN.md §9 "Ingest context").

:class:`IngestSession` implements :class:`~tagalot.themes.api.IngestContext` on one
connection inside a DB-writer transaction. It validates every call against the theme's
declarations (a mistake is an :class:`IngestError`: a theme bug to report, not a crash),
respects field provenance (extracted values never overwrite what the user edited), and
batches containment edges and search-index updates until :meth:`IngestSession.flush`.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, delete, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core import closure, fts
from tagalot.core.models import (
    Entity,
    EntityContains,
    EntityResource,
    FieldProvenance,
    FieldSource,
    utcnow,
)
from tagalot.core.theme_schema import EntityTable, ThemeSchema
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import EntityRef, Record, ResourceInfo

logger = logging.getLogger(__name__)

TITLE = "title"
"""The provenance name of the core ``title`` field."""


class IngestError(ValueError):
    """A theme asked for something its declarations don't allow (a theme bug)."""


@dataclass(frozen=True)
class IngestWarning:
    """A problem a theme reported about a file, or one the core found while ingesting."""

    root_id: str | None
    relpath: str | None
    message: str


@dataclass
class FlushReport:
    """What :meth:`IngestSession.flush` applied."""

    edges_added: int = 0
    edges_removed: int = 0
    entities_indexed: int = 0
    warnings: list[IngestWarning] = field(default_factory=list)


class IngestSession:
    """``ctx`` for ``Theme.ingest``, actions, and ``migrate`` on one connection."""

    def __init__(
        self,
        conn: Connection,
        schema: ThemeSchema,
        prepared: Mapping[int, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        self.conn = conn
        self.schema = schema
        self._prepared = prepared or {}
        self._options = dict(options) if options is not None else default_options(schema.theme)
        self.warnings: list[IngestWarning] = []
        self._added_edges: list[tuple[int, int]] = []
        self._removed_edges: list[tuple[int, int]] = []
        self._dirty: set[int] = set()
        self._containment = {
            (schema.theme.type_id_of(c.parent), schema.theme.type_id_of(c.child))
            for c in schema.theme.containment
        }

    # --- entities ---

    def upsert(
        self, type: type[ThemeEntity], key: str, *, title: str | None = None, **fields: Any
    ) -> EntityRef:
        """Find the ``type`` entity with this ingest key, or create it; set extracted values.

        Values the user edited (provenance ``user``) are left alone, title included.
        """
        entity_table = self._entity_table(type)
        unknown = sorted(set(fields) - {f.name for f in entity_table.fields})
        if unknown:
            raise IngestError(f"{type.__name__} has no field {', '.join(unknown)}")
        if not key:
            raise IngestError(f"{type.__name__}: an ingest key can't be empty")
        type_id = entity_table.type_id
        existing = self.conn.scalar(
            select(Entity.id).where(Entity.type == type_id, Entity.ingest_key == key)
        )
        if existing is None:
            entity_id = int(
                self.conn.execute(
                    insert(Entity)
                    .values(type=type_id, title=title if title is not None else key, ingest_key=key)
                    .returning(Entity.id)
                ).scalar_one()
            )
            self.conn.execute(insert(entity_table.table).values(id=entity_id, **fields))
            closure.add_entities(self.conn, [entity_id])
            self._mark_extracted(entity_id, ([TITLE] if title is not None else []) + list(fields))
        else:
            entity_id = existing
            self._apply_extracted(entity_id, entity_table, title, fields)
        self._dirty.add(entity_id)
        return EntityRef(entity_id, type_id)

    def update(self, entity: EntityRef, *, title: str | None = None, **fields: Any) -> None:
        """Set extracted values on a known entity; user-edited values are left alone."""
        entity_table = self._table_of(entity)
        unknown = sorted(set(fields) - {f.name for f in entity_table.fields})
        if unknown:
            raise IngestError(f"{entity_table.entity.__name__} has no field {', '.join(unknown)}")
        if self.conn.scalar(select(Entity.id).where(Entity.id == entity.id)) is None:
            raise IngestError(f"entity {entity.id} no longer exists")
        self._apply_extracted(entity.id, entity_table, title, fields)
        self._dirty.add(entity.id)

    def option(self, name: str) -> Any:
        """A theme option's value for the root being scanned (its default elsewhere)."""
        try:
            return self._options[name]
        except KeyError:
            raise IngestError(
                f"the {self.schema.theme.id!r} theme has no option {name!r}"
            ) from None

    def contents(self, entity: EntityRef) -> list[EntityRef]:
        """Direct children, counting this batch's ``contain``/``uncontain`` not yet flushed."""
        rows = self.conn.execute(
            select(EntityContains.child_id).where(EntityContains.parent_id == entity.id)
        ).scalars()
        children = dict.fromkeys(rows)
        for parent, child in self._removed_edges:
            if parent == entity.id:
                children.pop(child, None)
        for parent, child in self._added_edges:
            if parent == entity.id:
                children[child] = None
        if not children:
            return []
        found = self.conn.execute(select(Entity.id, Entity.type).where(Entity.id.in_(children)))
        types = {row.id: row.type for row in found}
        return [EntityRef(c, types[c]) for c in children if c in types]

    def linked(self, entity: EntityRef, role: str | None = None) -> list[int]:
        query = select(EntityResource.resource_id).where(EntityResource.entity_id == entity.id)
        if role is not None:
            query = query.where(EntityResource.role == role)
        return list(self.conn.execute(query.order_by(EntityResource.sort_order)).scalars())

    def delete(self, entity: EntityRef) -> None:
        """Delete an entity and everything linked to it (not its resources)."""
        self._table_of(entity)
        # Pending edges first, so detaching sees the containment as it now stands.
        self._added_edges = [e for e in self._added_edges if entity.id not in e]
        self._removed_edges = [e for e in self._removed_edges if entity.id not in e]
        self._flush_edges(FlushReport())
        closure.detach(self.conn, [entity.id])
        self.conn.execute(delete(Entity).where(Entity.id == entity.id))
        self._dirty.add(entity.id)  # its search row goes at flush

    def prepared(self, resource: ResourceInfo | int) -> Any:
        """What the theme's ``prepare`` returned for ``resource``, or ``None``."""
        return self._prepared.get(resource.id if isinstance(resource, ResourceInfo) else resource)

    def entities_of(self, resource: ResourceInfo | int, role: str | None = None) -> list[EntityRef]:
        """Entities linked to a resource (in ``role``, if given), of this theme's types."""
        resource_id = resource.id if isinstance(resource, ResourceInfo) else resource
        query = (
            select(Entity.id, Entity.type)
            .join(EntityResource, EntityResource.entity_id == Entity.id)
            .where(EntityResource.resource_id == resource_id)
            .distinct()
            .order_by(Entity.id)
        )
        if role is not None:
            query = query.where(EntityResource.role == role)
        types = {t.type_id for t in self.schema.entities.values()}
        return [EntityRef(i, t) for i, t in self.conn.execute(query) if t in types]

    def find(self, type: type[ThemeEntity], **equals: Any) -> list[EntityRef]:
        """Entities of ``type`` whose fields (or ``title``, ``ingest_key``) equal the values."""
        entity_table = self._entity_table(type)
        table = entity_table.table
        query = (
            select(Entity.id)
            .join(table, table.c.id == Entity.id)
            .where(Entity.type == entity_table.type_id)
            .order_by(Entity.id)
        )
        for name, value in equals.items():
            if name in (TITLE, "ingest_key"):
                query = query.where(getattr(Entity, name) == value)
            elif name in table.c:
                query = query.where(table.c[name] == value)
            else:
                raise IngestError(f"{type.__name__} has no field {name!r}")
        return [EntityRef(i, entity_table.type_id) for i in self.conn.scalars(query)]

    def get(self, entity: EntityRef) -> Record:
        """The entity's current title, fields, and ``extra``."""
        entity_table = self._table_of(entity)
        table = entity_table.table
        row = self.conn.execute(
            select(
                Entity.title.label("_title"),
                Entity.extra.label("_extra"),
                *(table.c[f.name] for f in entity_table.fields),
            )
            .join(table, table.c.id == Entity.id)
            .where(Entity.id == entity.id)
        ).one_or_none()
        if row is None:
            raise IngestError(f"entity {entity.id} no longer exists")
        values = row._mapping
        fields = {f.name: values[f.name] for f in entity_table.fields}
        return Record(entity, values["_title"], fields, dict(values["_extra"] or {}))

    # --- role links ---

    def link(
        self, entity: EntityRef, resource: ResourceInfo | int, role: str, sort_order: int = 0
    ) -> None:
        """Link a resource in a role. A single-valued role replaces its previous resource."""
        declared = self._role(entity, role)
        resource_id = resource.id if isinstance(resource, ResourceInfo) else resource
        if not declared.many:
            self.conn.execute(
                delete(EntityResource).where(
                    EntityResource.entity_id == entity.id,
                    EntityResource.role == role,
                    EntityResource.resource_id != resource_id,
                )
            )
        self.conn.execute(
            sqlite_insert(EntityResource)
            .values(entity_id=entity.id, resource_id=resource_id, role=role, sort_order=sort_order)
            .on_conflict_do_update(
                index_elements=["entity_id", "resource_id", "role"],
                set_={"sort_order": sort_order},
            )
        )
        self._forget_thumbnail(entity)

    def unlink(self, entity: EntityRef, resource: ResourceInfo | int, role: str) -> None:
        self._role(entity, role)
        resource_id = resource.id if isinstance(resource, ResourceInfo) else resource
        self.conn.execute(
            delete(EntityResource).where(
                EntityResource.entity_id == entity.id,
                EntityResource.resource_id == resource_id,
                EntityResource.role == role,
            )
        )
        self._forget_thumbnail(entity)

    # --- containment (applied at flush) ---

    def contain(self, parent: EntityRef, child: EntityRef) -> None:
        self._check_containment(parent, child)
        self._added_edges.append((parent.id, child.id))

    def uncontain(self, parent: EntityRef, child: EntityRef) -> None:
        self._check_containment(parent, child)
        self._removed_edges.append((parent.id, child.id))

    # --- relationships ---

    def relate(self, name: str, a: EntityRef, b: EntityRef) -> None:
        """Link ``a`` and ``b``; for a ``many=False`` relationship this replaces ``b``'s
        previous partner."""
        table, rel = self._relationship(name, a, b)
        if not rel.many:
            self.conn.execute(delete(table).where(table.c.b_id == b.id, table.c.a_id != a.id))
        self.conn.execute(insert(table).values(a_id=a.id, b_id=b.id).prefix_with("OR IGNORE"))

    def unrelate(self, name: str, a: EntityRef, b: EntityRef) -> None:
        table, _ = self._relationship(name, a, b)
        self.conn.execute(delete(table).where(table.c.a_id == a.id, table.c.b_id == b.id))

    # --- reporting ---

    def warn(self, resource: ResourceInfo | None, message: str) -> None:
        """Record a problem with a file for the activity panel."""
        warning = IngestWarning(
            resource.root_id if resource else None,
            resource.relpath if resource else None,
            message,
        )
        self.warnings.append(warning)
        logger.warning("Ingest: %s: %s", warning.relpath or "(no file)", message)

    # --- finishing ---

    def flush(self) -> FlushReport:
        """Apply batched containment changes and refresh the search index for touched
        entities. Call before the transaction commits; safe to call more than once."""
        report = FlushReport()
        self._flush_edges(report)
        if self._dirty:
            fts.sync_entities(self.conn, self._dirty, theme_text_source(self.schema))
            report.entities_indexed = len(self._dirty)
            self._dirty = set()
        report.warnings = list(self.warnings)
        return report

    # --- internals ---

    def _flush_edges(self, report: FlushReport) -> None:
        if self._added_edges or self._removed_edges:
            result = closure.apply(self.conn, self._added_edges, self._removed_edges)
            report.edges_added += len(result.added)
            report.edges_removed += len(result.removed)
            for (parent, child), reason in result.rejected:
                self.warn(None, f"containment {parent} -> {child} rejected: {reason}")
            self._added_edges, self._removed_edges = [], []

    def _entity_table(self, type: type[ThemeEntity]) -> EntityTable:
        try:
            return self.schema.entities[type]
        except KeyError:
            raise IngestError(
                f"{getattr(type, '__name__', type)!r} is not an entity of the "
                f"{self.schema.theme.id!r} theme"
            ) from None

    def _table_of(self, entity: EntityRef) -> EntityTable:
        try:
            return self.schema.by_type_id(entity.type)
        except KeyError:
            raise IngestError(f"{entity.type!r} is not a type of this theme") from None

    def _forget_thumbnail(self, entity: EntityRef) -> None:
        """Its links changed, so its thumbnail source is chosen afresh (DESIGN.md §10)."""
        self.conn.execute(
            update(Entity)
            .where(Entity.id == entity.id, Entity.thumb_resource_id.is_not(None))
            .values(thumb_resource_id=None)
        )

    def _role(self, entity: EntityRef, role: str) -> Any:
        entity_table = self._table_of(entity)
        for declared in entity_table.entity.roles:
            if declared.name == role:
                return declared
        raise IngestError(f"{entity_table.entity.__name__} has no role {role!r}")

    def _check_containment(self, parent: EntityRef, child: EntityRef) -> None:
        self._table_of(parent)
        self._table_of(child)
        if (parent.type, child.type) not in self._containment:
            raise IngestError(f"the theme doesn't declare that {parent.type} contains {child.type}")

    def _relationship(self, name: str, a: EntityRef, b: EntityRef) -> tuple[Any, Any]:
        rel_table = self.schema.relationships.get(name)
        if rel_table is None:
            raise IngestError(f"the theme has no relationship {name!r}")
        rel = rel_table.relationship
        theme = self.schema.theme
        if (a.type, b.type) != (theme.type_id_of(rel.a), theme.type_id_of(rel.b)):
            raise IngestError(
                f"relationship {name!r} links {rel.a.__name__} to {rel.b.__name__}, "
                f"not {a.type} to {b.type}"
            )
        return rel_table.table, rel

    def _apply_extracted(
        self,
        entity_id: int,
        entity_table: EntityTable,
        title: str | None,
        fields: Mapping[str, Any],
    ) -> None:
        user_owned = self._user_fields(entity_id)
        if title is not None and TITLE not in user_owned:
            self.conn.execute(update(Entity).where(Entity.id == entity_id).values(title=title))
            self._mark_extracted(entity_id, [TITLE])
        writable = {k: v for k, v in fields.items() if k not in user_owned}
        if writable:
            self.conn.execute(
                update(entity_table.table)
                .where(entity_table.table.c.id == entity_id)
                .values(**writable)
            )
            self._mark_extracted(entity_id, writable)
            self.conn.execute(
                update(Entity).where(Entity.id == entity_id).values(updated_at=utcnow())
            )

    def _user_fields(self, entity_id: int) -> set[str]:
        return set(
            self.conn.scalars(
                select(FieldProvenance.field).where(
                    FieldProvenance.entity_id == entity_id,
                    FieldProvenance.source == FieldSource.USER,
                )
            )
        )

    def _mark_extracted(self, entity_id: int, names: Iterable[str]) -> None:
        rows = [
            {
                "entity_id": entity_id,
                "field": n,
                "source": FieldSource.EXTRACTED,
                "updated_at": utcnow(),
            }
            for n in names
        ]
        if not rows:
            return
        stmt = sqlite_insert(FieldProvenance).values(rows)
        self.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["entity_id", "field"],
                set_={"source": stmt.excluded.source, "updated_at": stmt.excluded.updated_at},
                where=FieldProvenance.source != FieldSource.USER,
            )
        )


def default_options(theme: type[Any]) -> dict[str, Any]:
    """Every option a theme declares, at its default."""
    return {o.name: o.default for o in getattr(theme, "options", ())}


def theme_text_source(schema: ThemeSchema) -> fts.TextSource:
    """A :data:`~tagalot.core.fts.TextSource` giving each entity's ``search="text"`` values."""

    def source(conn: Connection, ids: Sequence[int]) -> Mapping[int, Sequence[str | None]]:
        result: dict[int, list[str | None]] = {}
        for entity_table in schema.entities.values():
            columns = [
                entity_table.table.c[f.name]
                for f in entity_table.fields
                if f.spec.search == "text" and f.type is str
            ]
            if not columns:
                continue
            rows = conn.execute(
                select(entity_table.table.c.id, *columns).where(entity_table.table.c.id.in_(ids))
            )
            for entity_id, *values in rows:
                result[entity_id] = list(values)
        return result

    return source
