"""Whole-entity snapshots, for undoing changes a theme action made (DESIGN.md §9 "Actions").

A theme action may create, edit, link, contain, relate, and delete entities. To undo it,
:class:`ChangeRecorder` snapshots every entity the action touches *before its first write*
(``None`` for one the action creates), and again at the end. :func:`restore_states` puts a
set of snapshots back exactly: an entity snapshotted as ``None`` is deleted, a deleted one
is recreated with the same id, and each one's row, theme fields, provenance, file links,
tags, containment edges, and relationships become what they were.

Edges and relationships belong to both ends, so an entity is touched whenever an edge or
relationship of its changes; restoring one end's snapshot then gives the edge back without
disturbing entities the action never touched.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, delete, insert, literal, or_, select, update

from tagalot.core import closure, fts
from tagalot.core.ingest import theme_text_source
from tagalot.core.models import (
    Entity,
    EntityContains,
    EntityResource,
    EntityTag,
    FieldProvenance,
    FieldSource,
    UserContains,
    UserOrder,
    UserRelation,
)
from tagalot.core.theme_schema import ThemeSchema


@dataclass(frozen=True)
class EntitySnapshot:
    """Everything stored about one entity (derived data, the closure and search index,
    is rebuilt from it)."""

    type: str
    title: str
    ingest_key: str | None
    extra: dict[str, Any]
    created_at: datetime
    values: dict[str, Any]
    """The theme table's fields."""
    provenance: frozenset[tuple[str, FieldSource]]
    links: frozenset[tuple[int, str, int, bool]]
    """(resource id, role, sort order, made by the user)."""
    tags: frozenset[tuple[int, datetime]]
    """(tag id, when it was added)."""
    parents: frozenset[int]
    children: frozenset[int]
    relations: frozenset[tuple[str, int, int, int | None]]
    """(relationship name, a id, b id, position) for relationships this entity is either
    end of; the position is ``None`` but in ordered relationships (#317)."""
    user_relations: frozenset[tuple[str, int, int, bool]] = frozenset()
    """(name, a id, b id, added) the user's records about those (#260)."""
    user_contains: frozenset[tuple[int, int, bool]] = frozenset()
    """(parent id, child id, added): containment the user changed by hand (#329)."""
    user_orders: frozenset[str] = frozenset()
    """Ordered relationships whose items of this entity the user ordered by hand (#317)."""


States = Mapping[int, EntitySnapshot | None]
"""Entity id -> its snapshot, or ``None`` when it doesn't exist."""


def snapshot_entities(
    conn: Connection, schema: ThemeSchema, ids: Iterable[int]
) -> dict[int, EntitySnapshot | None]:
    """Snapshot these entities; ids that don't exist map to ``None``."""
    wanted = list(dict.fromkeys(ids))
    found: dict[int, EntitySnapshot | None] = dict.fromkeys(wanted)
    if not wanted:
        return found
    rows = conn.execute(
        select(
            Entity.id,
            Entity.type,
            Entity.title,
            Entity.ingest_key,
            Entity.extra,
            Entity.created_at,
        ).where(Entity.id.in_(wanted))
    ).all()
    for entity_id, type_id, title, key, extra, created in rows:
        found[entity_id] = EntitySnapshot(
            type=type_id,
            title=title,
            ingest_key=key,
            extra=dict(extra or {}),
            created_at=created,
            values=_values(conn, schema, entity_id, type_id),
            provenance=_rows(
                conn.execute(
                    select(FieldProvenance.field, FieldProvenance.source).where(
                        FieldProvenance.entity_id == entity_id
                    )
                )
            ),
            links=_rows(
                conn.execute(
                    select(
                        EntityResource.resource_id,
                        EntityResource.role,
                        EntityResource.sort_order,
                        EntityResource.by_user,
                    ).where(EntityResource.entity_id == entity_id)
                )
            ),
            tags=_rows(
                conn.execute(
                    select(EntityTag.tag_id, EntityTag.added_at).where(
                        EntityTag.entity_id == entity_id
                    )
                )
            ),
            parents=frozenset(
                conn.scalars(
                    select(EntityContains.parent_id).where(EntityContains.child_id == entity_id)
                )
            ),
            children=frozenset(
                conn.scalars(
                    select(EntityContains.child_id).where(EntityContains.parent_id == entity_id)
                )
            ),
            relations=frozenset(_relations(conn, schema, entity_id)),
            user_relations=_rows(
                conn.execute(
                    select(
                        UserRelation.name, UserRelation.a_id, UserRelation.b_id, UserRelation.added
                    ).where(or_(UserRelation.a_id == entity_id, UserRelation.b_id == entity_id))
                )
            ),
            user_orders=frozenset(
                conn.scalars(select(UserOrder.name).where(UserOrder.b_id == entity_id))
            ),
            user_contains=_rows(
                conn.execute(
                    select(UserContains.parent_id, UserContains.child_id, UserContains.added).where(
                        or_(UserContains.parent_id == entity_id, UserContains.child_id == entity_id)
                    )
                )
            ),
        )
    return found


def restore_states(conn: Connection, schema: ThemeSchema, states: States) -> None:
    """Make these entities exactly as snapshotted (see the module docstring)."""
    ids = list(states)
    existing = set(conn.scalars(select(Entity.id).where(Entity.id.in_(ids))))
    gone = [i for i, s in states.items() if s is None and i in existing]
    if gone:
        closure.detach(conn, gone)
        conn.execute(delete(Entity).where(Entity.id.in_(gone)))
    present = {i: s for i, s in states.items() if s is not None}

    # Rows first (recreated ones keep their id), then what hangs off them.
    for entity_id, state in present.items():
        table = schema.by_type_id(state.type).table
        if entity_id in existing:
            conn.execute(
                update(Entity)
                .where(Entity.id == entity_id)
                .values(
                    title=state.title,
                    ingest_key=state.ingest_key,
                    extra=state.extra,
                    thumb_resource_id=None,
                )
            )
            if state.values:
                conn.execute(update(table).where(table.c.id == entity_id).values(**state.values))
        else:
            conn.execute(
                insert(Entity).values(
                    id=entity_id,
                    type=state.type,
                    title=state.title,
                    ingest_key=state.ingest_key,
                    extra=state.extra,
                    created_at=state.created_at,
                )
            )
            conn.execute(insert(table).values(id=entity_id, **state.values))
            closure.add_entities(conn, [entity_id])
    if present:
        _replace_rows(conn, present)
        _restore_relations(conn, schema, present)
        _restore_user_relations(conn, present)
        _restore_user_orders(conn, present)
        _restore_user_contains(conn, present)
        _restore_edges(conn, present)
    fts.sync_entities(conn, ids, theme_text_source(schema))


class ChangeRecorder:
    """Snapshots entities before an action first writes to them; see the module docstring."""

    def __init__(self, conn: Connection, schema: ThemeSchema) -> None:
        self.conn = conn
        self.schema = schema
        self.before: dict[int, EntitySnapshot | None] = {}

    def touch(self, ids: Iterable[int]) -> None:
        """About to change these entities: remember how they are, if not yet."""
        new = [i for i in dict.fromkeys(ids) if i not in self.before]
        if new:
            self.before.update(snapshot_entities(self.conn, self.schema, new))

    def created(self, entity_id: int) -> None:
        """An entity the action made: undoing deletes it."""
        self.before.setdefault(entity_id, None)

    def after(self) -> dict[int, EntitySnapshot | None]:
        """The touched entities as they are now."""
        return snapshot_entities(self.conn, self.schema, self.before)


# --- helpers ---


def _rows(result: Iterable[Any]) -> frozenset[Any]:
    return frozenset(tuple(row) for row in result)


def _values(conn: Connection, schema: ThemeSchema, entity_id: int, type_id: str) -> dict[str, Any]:
    try:
        table = schema.by_type_id(type_id).table
    except KeyError:
        return {}
    row = conn.execute(select(table).where(table.c.id == entity_id)).first()
    return {k: v for k, v in row._mapping.items() if k != "id"} if row else {}


def _relations(
    conn: Connection, schema: ThemeSchema, entity_id: int
) -> Iterable[tuple[str, int, int, int | None]]:
    for name, rel in schema.relationships.items():
        t = rel.table
        position = t.c.position if rel.relationship.ordered else literal(None)
        for a, b, at in conn.execute(
            select(t.c.a_id, t.c.b_id, position).where(
                or_(t.c.a_id == entity_id, t.c.b_id == entity_id)
            )
        ):
            yield name, a, b, at


def _replace_rows(conn: Connection, present: Mapping[int, EntitySnapshot]) -> None:
    """Provenance, file links, and tags: each entity's rows become the snapshot's."""
    ids = list(present)
    conn.execute(delete(FieldProvenance).where(FieldProvenance.entity_id.in_(ids)))
    conn.execute(delete(EntityResource).where(EntityResource.entity_id.in_(ids)))
    conn.execute(delete(EntityTag).where(EntityTag.entity_id.in_(ids)))
    provenance = [
        {"entity_id": i, "field": name, "source": source}
        for i, s in present.items()
        for name, source in s.provenance
    ]
    links = [
        {"entity_id": i, "resource_id": r, "role": role, "sort_order": order, "by_user": mine}
        for i, s in present.items()
        for r, role, order, mine in s.links
    ]
    tags = [
        {"entity_id": i, "tag_id": tag, "added_at": added}
        for i, s in present.items()
        for tag, added in s.tags
    ]
    for model, rows in ((FieldProvenance, provenance), (EntityResource, links), (EntityTag, tags)):
        if rows:
            # A resource or tag deleted since can't be linked again; skip those rows.
            conn.execute(insert(model).prefix_with("OR IGNORE"), rows)


def _restore_relations(
    conn: Connection, schema: ThemeSchema, present: Mapping[int, EntitySnapshot]
) -> None:
    ids = list(present)
    wanted = {r for s in present.values() for r in s.relations}
    for name, rel in schema.relationships.items():
        t = rel.table
        conn.execute(delete(t).where(or_(t.c.a_id.in_(ids), t.c.b_id.in_(ids))))
        if rel.relationship.ordered:
            rows = [{"a_id": a, "b_id": b, "position": p} for n, a, b, p in wanted if n == name]
        else:
            rows = [{"a_id": a, "b_id": b} for n, a, b, _ in wanted if n == name]
        if rows:
            conn.execute(insert(t).prefix_with("OR IGNORE"), rows)


def _restore_user_relations(conn: Connection, present: Mapping[int, EntitySnapshot]) -> None:
    ids = list(present)
    conn.execute(
        delete(UserRelation).where(or_(UserRelation.a_id.in_(ids), UserRelation.b_id.in_(ids)))
    )
    rows = {r for s in present.values() for r in s.user_relations}
    if rows:
        conn.execute(
            insert(UserRelation).prefix_with("OR IGNORE"),  # an item gone since: skipped
            [{"name": n, "a_id": a, "b_id": b, "added": added} for n, a, b, added in rows],
        )


def _restore_user_contains(conn: Connection, present: Mapping[int, EntitySnapshot]) -> None:
    ids = list(present)
    conn.execute(
        delete(UserContains).where(
            or_(UserContains.parent_id.in_(ids), UserContains.child_id.in_(ids))
        )
    )
    rows = {r for s in present.values() for r in s.user_contains}
    if rows:
        conn.execute(
            insert(UserContains).prefix_with("OR IGNORE"),  # an item gone since: skipped
            [{"parent_id": p, "child_id": c, "added": added} for p, c, added in rows],
        )


def _restore_user_orders(conn: Connection, present: Mapping[int, EntitySnapshot]) -> None:
    ids = list(present)
    conn.execute(delete(UserOrder).where(UserOrder.b_id.in_(ids)))
    rows = [{"name": n, "b_id": i} for i, s in present.items() for n in s.user_orders]
    if rows:
        conn.execute(insert(UserOrder), rows)


def _restore_edges(conn: Connection, present: Mapping[int, EntitySnapshot]) -> None:
    ids = list(present)
    current: set[tuple[int, int]] = set(
        _rows(
            conn.execute(
                select(EntityContains.parent_id, EntityContains.child_id).where(
                    or_(EntityContains.parent_id.in_(ids), EntityContains.child_id.in_(ids))
                )
            )
        )
    )
    wanted: set[tuple[int, int]] = set()
    for i, s in present.items():
        wanted |= {(p, i) for p in s.parents}
        wanted |= {(i, c) for c in s.children}
    alive = set(
        conn.scalars(select(Entity.id).where(Entity.id.in_({e for edge in wanted for e in edge})))
    )
    wanted = {(p, c) for p, c in wanted if p in alive and c in alive}
    closure.apply(conn, added=sorted(wanted - current), removed=sorted(current - wanted))


def changed(before: States, after: States) -> Sequence[int]:
    """Ids whose snapshot differs."""
    return [i for i in before if before[i] != after.get(i)]
