"""Maintenance of the ``entity_ancestor`` closure table (DESIGN.md §5, §6).

Containment is a DAG. ``entity_ancestor`` holds every ``(entity, ancestor)`` pair with the
minimum depth, including a depth-0 self row per entity. Removing an edge can drop an ancestor
or lengthen its shortest path, which the table alone can't tell, so every change uses one
strategy:

1. collect the affected nodes (each changed edge's child and its current descendants);
2. apply the edge changes to ``entity_contains``, rejecting edges that would make a cycle;
3. delete the affected nodes' rows and rebuild them with one recursive CTE over the edges.

All functions take a :class:`~sqlalchemy.Connection` and run inside a DB-writer transaction.
"""

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from sqlalchemy import (
    Column,
    Connection,
    Integer,
    MetaData,
    Select,
    Table,
    delete,
    func,
    insert,
    literal,
    or_,
    select,
    tuple_,
)

from tagalot.core.models import Entity, EntityAncestor, EntityContains

logger = logging.getLogger(__name__)

MAX_DEPTH = 64
"""Guard against corrupt data: the rebuild never follows longer chains than this."""

BATCH_SIZE = 500

Edge = tuple[int, int]
"""``(parent_id, child_id)``."""

# Affected ids live in a per-connection temporary table, so any number of them can be used
# without hitting SQLite's limit on bound parameters.
_temp = MetaData()
_affected = Table(
    "closure_affected",
    _temp,
    Column("id", Integer, primary_key=True),
    prefixes=["TEMPORARY"],
)


@dataclass
class ClosureResult:
    """What :func:`apply` changed."""

    added: list[Edge] = field(default_factory=list)
    removed: list[Edge] = field(default_factory=list)
    rejected: list[tuple[Edge, str]] = field(default_factory=list)
    """Edges refused, with why (a theme ingester bug to report, not a reason to stop)."""
    affected: int = 0
    """How many entities had their ancestor rows rebuilt."""


def apply(
    conn: Connection, added: Iterable[Edge] = (), removed: Iterable[Edge] = ()
) -> ClosureResult:
    """Add and remove containment edges and bring ``entity_ancestor`` up to date.

    Removals happen first. Additions are then checked in order, each against the graph
    including the ones accepted before it: an edge from an entity to itself, or to one of
    its ancestors, would create a cycle and is rejected. Existing edges and missing ones
    are skipped.
    """
    result = ClosureResult()
    wanted_removed = list(dict.fromkeys(removed))
    wanted_added = list(dict.fromkeys(added))

    existing = _existing_edges(conn, wanted_removed + wanted_added)
    to_remove = [e for e in wanted_removed if e in existing]
    to_add = [e for e in wanted_added if e not in existing or e in to_remove]

    # 1. Affected nodes, from the closure as it is before any edge changes.
    roots = {child for _, child in to_remove} | {child for _, child in to_add}
    affected = _with_descendants(conn, roots)

    # 2. Edge changes.
    for batch in _chunks(to_remove):
        conn.execute(
            delete(EntityContains).where(
                tuple_(EntityContains.parent_id, EntityContains.child_id).in_(batch)
            )
        )
    result.removed = to_remove
    # Only an edge into an entity that already has children can close a cycle (the child
    # must reach the parent going down). During ingest most children are new leaves, so the
    # recursive check is usually skipped; accepted edges are inserted in batches, flushed
    # before any check so it sees them.
    has_children: set[int] = (
        set(conn.scalars(select(EntityContains.parent_id).distinct())) if to_add else set()
    )
    pending: list[Edge] = []
    for parent, child in to_add:
        reason: str | None = None
        if parent == child:
            reason = "an entity can't contain itself"
        elif child in has_children:
            _insert_edges(conn, pending)
            pending = []
            reason = _cycle_reason(conn, parent, child)
        if reason is not None:
            result.rejected.append(((parent, child), reason))
            logger.warning("Rejected containment edge %d -> %d: %s", parent, child, reason)
            continue
        pending.append((parent, child))
        has_children.add(parent)
        result.added.append((parent, child))
    _insert_edges(conn, pending)

    # 3. Rebuild the affected rows.
    result.affected = _rebuild(conn, affected)
    return result


def add_entities(conn: Connection, entity_ids: Iterable[int]) -> None:
    """Give new entities their depth-0 self row (needed even without any edges)."""
    rows = [{"entity_id": i, "ancestor_id": i, "depth": 0} for i in dict.fromkeys(entity_ids)]
    for batch in _chunks(rows):
        conn.execute(insert(EntityAncestor).prefix_with("OR IGNORE"), list(batch))


def detach(conn: Connection, entity_ids: Iterable[int]) -> ClosureResult:
    """Remove every edge touching these entities, updating their former descendants.

    Call this before deleting entities: the foreign-key cascades remove the entities' own
    rows but not transitive ones (in A -> B -> C, deleting B would leave C under A).
    """
    ids = list(dict.fromkeys(entity_ids))
    edges: list[Edge] = []
    for batch in _chunks(ids):
        rows = conn.execute(
            select(EntityContains.parent_id, EntityContains.child_id).where(
                or_(EntityContains.parent_id.in_(batch), EntityContains.child_id.in_(batch))
            )
        )
        edges.extend((p, c) for p, c in rows)
    return apply(conn, removed=edges)


def _existing_edges(conn: Connection, edges: Sequence[Edge]) -> set[Edge]:
    found: set[Edge] = set()
    for batch in _chunks(list(dict.fromkeys(edges))):
        rows = conn.execute(
            select(EntityContains.parent_id, EntityContains.child_id).where(
                tuple_(EntityContains.parent_id, EntityContains.child_id).in_(batch)
            )
        )
        found.update((p, c) for p, c in rows)
    return found


def _with_descendants(conn: Connection, roots: set[int]) -> set[int]:
    result = set(roots)
    for batch in _chunks(sorted(roots)):
        result.update(
            conn.scalars(
                select(EntityAncestor.entity_id).where(EntityAncestor.ancestor_id.in_(batch))
            )
        )
    return result


def _insert_edges(conn: Connection, edges: Sequence[Edge]) -> None:
    for batch in _chunks(edges):
        conn.execute(insert(EntityContains), [{"parent_id": p, "child_id": c} for p, c in batch])


def _cycle_reason(conn: Connection, parent: int, child: int) -> str | None:
    """Why ``parent -> child`` would make a cycle, or ``None``. Walks the edges, not the
    closure, so edges accepted earlier in the same batch count."""
    if parent == child:
        return "an entity can't contain itself"
    up = select(literal(parent).label("id")).cte("up", recursive=True)
    up = up.union(select(EntityContains.parent_id).join(up, EntityContains.child_id == up.c.id))
    if conn.scalar(select(literal(1)).select_from(up).where(up.c.id == child).limit(1)):
        return "it would make a cycle (the child already contains the parent)"
    return None


def rebuild_all(conn: Connection) -> int:
    """Recompute the whole closure from ``entity_contains`` (the repair action, and the
    reference the incremental :func:`apply` is tested against). Returns the row count."""
    conn.execute(delete(EntityAncestor))
    _load_affected(conn, None)
    conn.execute(insert(EntityAncestor).from_select(_COLUMNS, _correct_rows()))
    conn.execute(delete(_affected))
    count = int(conn.scalar(select(func.count()).select_from(EntityAncestor)) or 0)
    logger.info("Rebuilt the containment closure (%d rows)", count)
    return count


@dataclass(frozen=True)
class ClosureCheck:
    """Differences between the stored closure and the one the edges imply."""

    missing: frozenset[tuple[int, int, int]]
    """Rows that should exist (entity, ancestor, depth) but don't, or have another depth."""
    extra: frozenset[tuple[int, int, int]]
    """Rows that exist but shouldn't, or have the wrong depth."""

    @property
    def ok(self) -> bool:
        return not self.missing and not self.extra


def verify(conn: Connection) -> ClosureCheck:
    """Compare the stored closure with a fresh computation, without changing anything."""
    _load_affected(conn, None)
    correct = {(e, a, d) for e, a, d in conn.execute(_correct_rows())}
    conn.execute(delete(_affected))
    stored = {
        (e, a, d)
        for e, a, d in conn.execute(
            select(EntityAncestor.entity_id, EntityAncestor.ancestor_id, EntityAncestor.depth)
        )
    }
    return ClosureCheck(missing=frozenset(correct - stored), extra=frozenset(stored - correct))


_COLUMNS = ["entity_id", "ancestor_id", "depth"]


def _rebuild(conn: Connection, entity_ids: set[int]) -> int:
    """Recompute every ``entity_ancestor`` row of these entities from ``entity_contains``."""
    if not entity_ids:
        return 0
    _load_affected(conn, entity_ids)
    conn.execute(delete(EntityAncestor).where(EntityAncestor.entity_id.in_(select(_affected.c.id))))
    conn.execute(insert(EntityAncestor).from_select(_COLUMNS, _correct_rows()))
    conn.execute(delete(_affected))
    return len(entity_ids)


def _load_affected(conn: Connection, entity_ids: set[int] | None) -> None:
    """Fill the temporary table with these ids, or with every entity for ``None``."""
    _affected.create(conn, checkfirst=True)
    conn.execute(delete(_affected))
    if entity_ids is None:
        conn.execute(insert(_affected).from_select(["id"], select(Entity.id)))
        return
    for batch in _chunks(sorted(entity_ids)):
        conn.execute(insert(_affected), [{"id": i} for i in batch])


def _correct_rows() -> Select[int, int, int]:
    """``(entity_id, ancestor_id, min depth)`` for every entity in the temporary table,
    walking ``entity_contains`` upward with a recursive CTE."""
    up = (
        select(
            _affected.c.id.label("entity_id"),
            _affected.c.id.label("ancestor_id"),
            literal(0).label("depth"),
        )
        .join(Entity, Entity.id == _affected.c.id)  # skip ids of deleted entities
        .cte("up", recursive=True)
    )
    up = up.union(
        select(up.c.entity_id, EntityContains.parent_id, up.c.depth + 1)
        .join(EntityContains, EntityContains.child_id == up.c.ancestor_id)
        .where(up.c.depth < MAX_DEPTH)
    )
    return select(up.c.entity_id, up.c.ancestor_id, func.min(up.c.depth)).group_by(
        up.c.entity_id, up.c.ancestor_id
    )


def _chunks[T](items: Sequence[T], size: int = BATCH_SIZE) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
