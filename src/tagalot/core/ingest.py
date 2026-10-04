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
from typing import Any, Protocol

from sqlalchemy import Connection, Insert, Table, bindparam, delete, func, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core import closure, fts
from tagalot.core.models import (
    Entity,
    EntityContains,
    EntityMerge,
    EntityMergeResource,
    EntityResource,
    FieldProvenance,
    FieldSource,
    UserRelation,
    utcnow,
)
from tagalot.core.theme_schema import EntityTable, ThemeSchema
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import EntityRef, Record, ResourceInfo

logger = logging.getLogger(__name__)

# Statements an ingest runs for every item, built once with bound parameters: building one
# afresh for each call costs more than SQLite takes to run it (#276).
_ENTITY: Table = Entity.__table__  # type: ignore[assignment]
_LINKS: Table = EntityResource.__table__  # type: ignore[assignment]
_PROVENANCE: Table = FieldProvenance.__table__  # type: ignore[assignment]
_BY_KEY = select(_ENTITY.c.id).where(
    _ENTITY.c.type == bindparam("type"), _ENTITY.c.ingest_key == bindparam("key")
)
_NEW_ENTITY = insert(_ENTITY)
_LINKED_TO = (
    select(_ENTITY.c.id, _ENTITY.c.type)
    .join(_LINKS, _LINKS.c.entity_id == _ENTITY.c.id)
    .where(_LINKS.c.resource_id == bindparam("resource"), _LINKS.c.by_user.is_(False))
    .distinct()
    .order_by(_ENTITY.c.id)
)
_LINKED_TO_IN_ROLE = _LINKED_TO.where(_LINKS.c.role == bindparam("role"))
_link = sqlite_insert(_LINKS)
_LINK = _link.on_conflict_do_update(
    index_elements=["entity_id", "resource_id", "role"],
    set_={"sort_order": _link.excluded.sort_order},
)
_mark = sqlite_insert(_PROVENANCE)
_MARK_EXTRACTED = _mark.on_conflict_do_update(
    index_elements=["entity_id", "field"],
    set_={"source": _mark.excluded.source, "updated_at": _mark.excluded.updated_at},
    where=_PROVENANCE.c.source != FieldSource.USER,
)


class Recorder(Protocol):
    """Told about entities before they change (``core.entity_state.ChangeRecorder``, which
    snapshots them so a theme action can be undone)."""

    def touch(self, ids: Iterable[int]) -> None: ...

    def created(self, entity_id: int) -> None: ...


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
        self.recorder: Recorder | None = None
        """Set by actions, to undo them; ingest and migrations leave it ``None``."""
        self._merged: set[int] = set()
        """Refs ``upsert`` gave for items the user merged away (#120): writes to them are
        dropped, so the item stays merged."""
        self._containment = {
            (schema.theme.type_id_of(c.parent), schema.theme.type_id_of(c.child))
            for c in schema.theme.containment
        }
        # What this session already knows, so each item costs few statements (#276). Only
        # this session writes while its transaction is open, so these stay true until it
        # changes them itself.
        self._next_id: int | None = None
        """The id the next new entity gets (looked up once, then counted up)."""
        self._keys: dict[tuple[str, str], int] = {}
        """``(type id, ingest key)`` -> entity id, for keys looked up or made this session."""
        self._created: set[int] = set()
        """Entities made this session: no user edits, no links by hand, no thumbnail yet."""
        self._linked_roles: set[tuple[int, str]] = set()
        """``(entity, role)`` linked this session, for entities made this session."""
        self._user_owned: dict[int, set[str]] = {}
        """Each entity's user-edited fields (``title`` included), looked up once."""
        self._written: dict[int, dict[str, Any]] = {}
        """Values this session wrote for each entity, so writing the same again is skipped."""
        self._new_closure: list[int] = []
        """New entities whose depth-0 closure rows are added at the next flush."""
        self._has_merges: bool | None = None
        """Whether the keep has merged items (see :meth:`_any_merges`)."""
        self._inserts: dict[str, Insert] = {}
        """Each theme table's insert, built once."""

    # --- entities ---

    def upsert(
        self, type: type[ThemeEntity], key: str, *, title: str | None = None, **fields: Any
    ) -> EntityRef:
        """Find the ``type`` entity with this ingest key, or create it; set extracted values.

        Values the user edited (provenance ``user``) are left alone, title included. An
        item the user merged into another (§13) isn't made again: the ref returned stands
        for it, and links, edges, relations, and values given for it are dropped.
        """
        entity_table = self._entity_table(type)
        unknown = sorted(set(fields) - {f.name for f in entity_table.fields})
        if unknown:
            raise IngestError(f"{type.__name__} has no field {', '.join(unknown)}")
        if not key:
            raise IngestError(f"{type.__name__}: an ingest key can't be empty")
        type_id = entity_table.type_id
        existing = self._keys.get((type_id, key))
        if existing is not None and existing in self._merged:
            return EntityRef(existing, type_id)
        if existing is None:
            existing = self.conn.scalar(_BY_KEY, {"type": type_id, "key": key})
        if existing is None:
            merged = (
                self.conn.scalar(
                    select(EntityMerge.merged_id)
                    .where(EntityMerge.type == type_id, EntityMerge.ingest_key == key)
                    .order_by(EntityMerge.merged_at.desc())
                    .limit(1)
                )
                if self._any_merges()
                else None
            )
            if merged is not None:
                self._merged.add(merged)
                self._keys[(type_id, key)] = merged
                return EntityRef(merged, type_id)
            if self._next_id is None:
                self._next_id = next_entity_id(self.conn)
            entity_id = self._next_id
            self._next_id += 1
            self.conn.execute(
                _NEW_ENTITY,
                {
                    "id": entity_id,
                    "type": type_id,
                    "title": title if title is not None else key,
                    "ingest_key": key,
                },
            )
            table = entity_table.table
            if table.name not in self._inserts:
                self._inserts[table.name] = insert(table)
            self.conn.execute(self._inserts[table.name], {"id": entity_id, **fields})
            self._new_closure.append(entity_id)
            self._created.add(entity_id)
            self._keys[(type_id, key)] = entity_id
            if self.recorder is not None:
                self.recorder.created(entity_id)
            self._mark_extracted(entity_id, ([TITLE] if title is not None else []) + list(fields))
            self._written[entity_id] = {**fields, **({TITLE: title} if title is not None else {})}
        else:
            entity_id = existing
            self._keys[(type_id, key)] = entity_id
            self._touch(entity_id)
            self._apply_extracted(entity_id, entity_table, title, fields)
        self._dirty.add(entity_id)
        return EntityRef(entity_id, type_id)

    def update(self, entity: EntityRef, *, title: str | None = None, **fields: Any) -> None:
        """Set extracted values on a known entity; user-edited values are left alone."""
        entity_table = self._table_of(entity)
        unknown = sorted(set(fields) - {f.name for f in entity_table.fields})
        if unknown:
            raise IngestError(f"{entity_table.entity.__name__} has no field {', '.join(unknown)}")
        if self._gone(entity):
            return
        if (
            entity.id not in self._created
            and self.conn.scalar(select(Entity.id).where(Entity.id == entity.id)) is None
        ):
            raise IngestError(f"entity {entity.id} no longer exists")
        self._touch(entity.id)
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
        if self._gone(entity):
            return []
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
        if self._gone(entity):
            return []
        query = select(EntityResource.resource_id).where(EntityResource.entity_id == entity.id)
        if role is not None:
            query = query.where(EntityResource.role == role)
        return list(self.conn.execute(query.order_by(EntityResource.sort_order)).scalars())

    def delete(self, entity: EntityRef) -> None:
        """Delete an entity and everything linked to it (not its resources). In a scan (no
        recorder), an item the user made by hand (no ingest key) is left alone (#260)."""
        self._table_of(entity)
        if self._gone(entity):
            return
        if self.recorder is None:
            key = self.conn.execute(select(Entity.ingest_key).where(Entity.id == entity.id)).first()
            if key is not None and key.ingest_key is None:
                return
        if self.recorder is not None:  # its neighbours lose their edges and relations to it
            self._touch(entity.id, *self._neighbours(entity.id))
        # Pending edges first, so detaching sees the containment as it now stands.
        self._added_edges = [e for e in self._added_edges if entity.id not in e]
        self._removed_edges = [e for e in self._removed_edges if entity.id not in e]
        self._flush_edges(FlushReport())
        closure.detach(self.conn, [entity.id])
        self.conn.execute(delete(Entity).where(Entity.id == entity.id))
        self._forget(entity.id)
        self._dirty.add(entity.id)  # its search row goes at flush

    def prepared(self, resource: ResourceInfo | int) -> Any:
        """What the theme's ``prepare`` returned for ``resource``, or ``None``."""
        return self._prepared.get(resource.id if isinstance(resource, ResourceInfo) else resource)

    def entities_of(self, resource: ResourceInfo | int, role: str | None = None) -> list[EntityRef]:
        """Entities linked to a resource (in ``role``, if given), of this theme's types.
        Links the user made by hand aren't included: the file isn't the theme's to read into
        that item. An item the user merged away that had the file is (§13): writes to it are
        dropped, so the theme doesn't make a new item for the file."""
        resource_id = resource.id if isinstance(resource, ResourceInfo) else resource
        rows = (
            self.conn.execute(_LINKED_TO, {"resource": resource_id})
            if role is None
            else self.conn.execute(_LINKED_TO_IN_ROLE, {"resource": resource_id, "role": role})
        )
        types = {t.type_id for t in self.schema.entities.values()}
        found = [EntityRef(i, t) for i, t in rows if t in types]
        if not self._any_merges():
            return found
        merged = (
            select(EntityMerge.merged_id, EntityMerge.type)
            .join(EntityMergeResource, EntityMergeResource.merged_id == EntityMerge.merged_id)
            .where(EntityMergeResource.resource_id == resource_id)
            .distinct()
            .order_by(EntityMerge.merged_id)
        )
        if role is not None:
            merged = merged.where(EntityMergeResource.role == role)
        for merged_id, type_id in self.conn.execute(merged):
            if type_id in types:
                self._merged.add(merged_id)
                found.append(EntityRef(merged_id, type_id))
        return found

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
        """The entity's current title, fields, and ``extra`` (for an item merged away, the
        item it was merged into's, or nothing)."""
        entity_table = self._table_of(entity)
        if self._gone(entity):
            into = merged_into(self.conn, [entity.id]).get(entity.id)
            if (
                into is None
                or self.conn.scalar(select(Entity.type).where(Entity.id == into)) != entity.type
            ):
                return Record(entity, "", {f.name: None for f in entity_table.fields}, {})
            found = self.get(EntityRef(into, entity.type))
            return Record(entity, found.title, found.fields, found.extra)
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
        """Link a resource in a role. A single-valued role replaces its previous resource,
        unless the user linked one there by hand: then this link is skipped."""
        declared = self._role(entity, role)
        resource_id = resource.id if isinstance(resource, ResourceInfo) else resource
        if self._gone(entity):
            return
        self._touch(entity.id)
        new = entity.id in self._created  # no links yet but this session's, none by hand
        if not declared.many and not new:
            by_hand = self.conn.scalar(
                select(EntityResource.resource_id).where(
                    EntityResource.entity_id == entity.id,
                    EntityResource.role == role,
                    EntityResource.by_user.is_(True),
                )
            )
            if by_hand is not None:
                return  # the user's file stays in the role
        if not declared.many and (not new or (entity.id, role) in self._linked_roles):
            self.conn.execute(
                delete(EntityResource).where(
                    EntityResource.entity_id == entity.id,
                    EntityResource.role == role,
                    EntityResource.resource_id != resource_id,
                )
            )
        if new:
            self._linked_roles.add((entity.id, role))
        self.conn.execute(
            _LINK,
            {
                "entity_id": entity.id,
                "resource_id": resource_id,
                "role": role,
                "sort_order": sort_order,
                "by_user": False,
            },
        )
        self._forget_thumbnail(entity)

    def unlink(self, entity: EntityRef, resource: ResourceInfo | int, role: str) -> None:
        self._role(entity, role)
        resource_id = resource.id if isinstance(resource, ResourceInfo) else resource
        if self._gone(entity):
            return
        self._touch(entity.id)
        self.conn.execute(
            delete(EntityResource).where(
                EntityResource.entity_id == entity.id,
                EntityResource.resource_id == resource_id,
                EntityResource.role == role,
                EntityResource.by_user.is_(False),  # the user's links stay
            )
        )
        self._forget_thumbnail(entity)

    # --- containment (applied at flush) ---

    # Within a batch the last contain/uncontain of an edge wins, so each cancels the other.

    def contain(self, parent: EntityRef, child: EntityRef) -> None:
        self._check_containment(parent, child)
        if self._gone(parent, child):
            return
        self._touch(parent.id, child.id)
        edge = (parent.id, child.id)
        self._removed_edges = [e for e in self._removed_edges if e != edge]
        if edge not in self._added_edges:
            self._added_edges.append(edge)

    def uncontain(self, parent: EntityRef, child: EntityRef) -> None:
        self._check_containment(parent, child)
        if self._gone(parent, child):
            return
        self._touch(parent.id, child.id)
        edge = (parent.id, child.id)
        self._added_edges = [e for e in self._added_edges if e != edge]
        if edge not in self._removed_edges:
            self._removed_edges.append(edge)

    # --- relationships ---

    def relate(self, name: str, a: EntityRef, b: EntityRef) -> None:
        """Link ``a`` and ``b``; for a ``many=False`` relationship this replaces ``b``'s
        previous partner."""
        table, rel = self._relationship(name, a, b)
        if self._gone(a, b) or self._by_user(name, a.id, b.id) is False:
            return  # merged away, or the user removed it (#260)
        if not rel.many:
            partner = self.conn.scalar(select(table.c.a_id).where(table.c.b_id == b.id))
            if partner not in (None, a.id) and self._by_user(name, partner, b.id):
                return  # b's one partner is the user's
        self._touch(a.id, b.id)
        if not rel.many:
            if self.recorder is not None:  # b's previous partner loses it
                self._touch(*self.conn.scalars(select(table.c.a_id).where(table.c.b_id == b.id)))
            self.conn.execute(delete(table).where(table.c.b_id == b.id, table.c.a_id != a.id))
        self.conn.execute(insert(table).values(a_id=a.id, b_id=b.id).prefix_with("OR IGNORE"))

    def unrelate(self, name: str, a: EntityRef, b: EntityRef) -> None:
        table, _ = self._relationship(name, a, b)
        if self._gone(a, b) or self._by_user(name, a.id, b.id):
            return  # merged away, or the user added it (#260)
        self._touch(a.id, b.id)
        self.conn.execute(delete(table).where(table.c.a_id == a.id, table.c.b_id == b.id))

    def related(self, name: str, entity: EntityRef) -> list[EntityRef]:
        """The entities related to ``entity`` through ``name``, from either side."""
        link = self.schema.relationships.get(name)
        if link is None:
            raise IngestError(f"no relationship {name!r} in the {self.schema.theme.id!r} theme")
        if self._gone(entity):
            return []
        t = link.table
        type_id = entity.type
        rel = link.relationship
        if type_id == self.schema.theme.type_id_of(rel.a):
            mine, other = t.c.a_id, t.c.b_id
        elif type_id == self.schema.theme.type_id_of(rel.b):
            mine, other = t.c.b_id, t.c.a_id
        else:
            raise IngestError(f"{type_id!r} isn't part of the {name!r} relationship")
        rows = self.conn.execute(
            select(Entity.id, Entity.type)
            .join(t, other == Entity.id)
            .where(mine == entity.id)
            .order_by(Entity.id)
        )
        return [EntityRef(i, t_) for i, t_ in rows]

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
        if self._new_closure:  # before edges: containment builds on these rows
            closure.add_entities(self.conn, self._new_closure)
            self._new_closure = []
        if self._added_edges or self._removed_edges:
            result = closure.apply(self.conn, self._added_edges, self._removed_edges)
            report.edges_added += len(result.added)
            report.edges_removed += len(result.removed)
            for (parent, child), reason in result.rejected:
                self.warn(None, f"containment {parent} -> {child} rejected: {reason}")
            self._added_edges, self._removed_edges = [], []

    def _by_user(self, name: str, a_id: int, b_id: int) -> bool | None:
        """Whether the user added (``True``) or removed (``False``) this relationship by
        hand, or ``None`` if they did neither."""
        added = self.conn.scalar(
            select(UserRelation.added).where(
                UserRelation.name == name, UserRelation.a_id == a_id, UserRelation.b_id == b_id
            )
        )
        return None if added is None else bool(added)

    def _gone(self, *entities: EntityRef) -> bool:
        """Any of these stands for an item merged away (see :meth:`upsert`)."""
        return any(e.id in self._merged for e in entities)

    def _touch(self, *ids: int) -> None:
        if self.recorder is not None:
            self.recorder.touch(ids)

    def _neighbours(self, entity_id: int) -> set[int]:
        """Entities with an edge (stored or pending) or a relationship to this one."""
        found = set(
            self.conn.scalars(
                select(EntityContains.child_id).where(EntityContains.parent_id == entity_id)
            )
        ) | set(
            self.conn.scalars(
                select(EntityContains.parent_id).where(EntityContains.child_id == entity_id)
            )
        )
        for parent, child in self._added_edges:
            if entity_id in (parent, child):
                found |= {parent, child}
        for rel in self.schema.relationships.values():
            t = rel.table
            found |= set(self.conn.scalars(select(t.c.b_id).where(t.c.a_id == entity_id)))
            found |= set(self.conn.scalars(select(t.c.a_id).where(t.c.b_id == entity_id)))
        found.discard(entity_id)
        return found

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
        if entity.id in self._created:
            return  # none chosen yet
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
        written = self._written.setdefault(entity_id, {})
        if title is not None and TITLE not in user_owned and written.get(TITLE, _UNSET) != title:
            self.conn.execute(update(Entity).where(Entity.id == entity_id).values(title=title))
            self._mark_extracted(entity_id, [TITLE])
            written[TITLE] = title
        writable = {
            k: v
            for k, v in fields.items()
            if k not in user_owned and written.get(k, _UNSET) != v  # not what's already set
        }
        if writable:
            written.update(writable)
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
        """The entity's user-edited fields. Ingest never makes a field the user's, so they
        are looked up once a session (none, for an entity made this session)."""
        if entity_id in self._created:
            return set()
        if entity_id not in self._user_owned:
            self._user_owned[entity_id] = set(
                self.conn.scalars(
                    select(FieldProvenance.field).where(
                        FieldProvenance.entity_id == entity_id,
                        FieldProvenance.source == FieldSource.USER,
                    )
                )
            )
        return self._user_owned[entity_id]

    def _any_merges(self) -> bool:
        """Whether this keep has any merged items (most never do), checked once a session:
        merging is the user's doing, never during an ingest."""
        if self._has_merges is None:
            self._has_merges = self.conn.scalar(select(EntityMerge.merged_id).limit(1)) is not None
        return self._has_merges

    def _forget(self, entity_id: int) -> None:
        """Drop what this session remembers about an entity it deleted."""
        self._keys = {k: v for k, v in self._keys.items() if v != entity_id}
        self._created.discard(entity_id)
        self._linked_roles = {(e, r) for e, r in self._linked_roles if e != entity_id}
        self._user_owned.pop(entity_id, None)
        self._written.pop(entity_id, None)
        if entity_id in self._new_closure:
            self._new_closure.remove(entity_id)

    def _mark_extracted(self, entity_id: int, names: Iterable[str]) -> None:
        now = utcnow()
        rows = [
            {"entity_id": entity_id, "field": n, "source": FieldSource.EXTRACTED, "updated_at": now}
            for n in names
        ]
        if rows:
            self.conn.execute(_MARK_EXTRACTED, rows)


_UNSET = object()
"""Not written this session (``None`` is a value that can be written)."""


def next_entity_id(conn: Connection) -> int:
    """The id for a new entity: past every entity's and every merged item's (§13), so a
    merge record never names a living item."""
    used = conn.scalar(select(func.max(Entity.id))) or 0
    merged = conn.scalar(select(func.max(EntityMerge.merged_id))) or 0
    return max(used, merged) + 1


def merged_into(conn: Connection, ids: Iterable[int]) -> dict[int, int]:
    """For each of these ids that was merged away, the item it is now part of, following
    later merges (§13); ids never merged are absent. The result may itself no longer exist
    (the kept item was deleted)."""
    found: dict[int, int] = {}
    for start in dict.fromkeys(ids):
        current, seen = start, {start}
        while True:
            into = conn.scalar(select(EntityMerge.into_id).where(EntityMerge.merged_id == current))
            if into is None or into in seen:
                break
            seen.add(into)
            current = into
        if current != start:
            found[start] = current
    return found


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
