"""Merging items into one (DESIGN.md §13; #120).

The kept item absorbs each other item's tags, files, containment edges, and relationships,
and the values the user set by hand: its fields and title where edited, and its own extra
fields. Where the items' hand-set values differ, the user picks one (:class:`Conflict`).
Files are never touched.

- **Files** move to the kept item as links the user made (``by_user``), so scans never move
  them back. In a role that holds one file and already has one, the other item's file is
  left linked to nothing; Triage lists it as unlinked.
- **The merged items** are deleted and remembered in ``entity_merge``: a scan that reads one
  of their files again doesn't recreate them (``IngestSession.upsert``), and saved searches
  and pages that name them follow to the kept item (:func:`~tagalot.core.ingest.merged_into`).
- **Undo:** a merge is one step (:class:`MergeChange`): whole-item snapshots of every item it
  touched, before and after, and the merge records it wrote.

:func:`plan_merge` says what a merge would do, for the dialog; :func:`merge_items` does it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, delete, func, insert, literal, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core import closure, fts
from tagalot.core.actions import ActionChange
from tagalot.core.detail import PATH_MARK
from tagalot.core.entity_state import ChangeRecorder, restore_states
from tagalot.core.ingest import TITLE, next_position, theme_text_source
from tagalot.core.models import (
    LAST_POSITION,
    Entity,
    EntityContains,
    EntityMerge,
    EntityMergeResource,
    EntityResource,
    EntityTag,
    FieldProvenance,
    FieldSource,
    Resource,
    Root,
    UserOrder,
    UserRelation,
    utcnow,
)
from tagalot.core.theme_schema import EntityTable, ThemeSchema
from tagalot.themes.api import entity_plural


class MergeError(ValueError):
    """A merge that can't be done; the message is for the user."""


@dataclass(frozen=True)
class Choice:
    """One item's value in a :class:`Conflict`."""

    entity_id: int
    text: str
    """The value as the page shows it (``""`` for none)."""


@dataclass(frozen=True)
class Conflict:
    """A value the user set by hand on more than one item, differently."""

    key: str
    """``title``, ``field:<name>``, or ``extra:<name>`` (the key :func:`merge_items` takes)."""
    label: str
    choices: tuple[Choice, ...]
    """One per item with a value of its own, the kept item first."""
    default: int
    """The entity id whose value wins unless the user picks another."""


@dataclass(frozen=True)
class MergePlan:
    keep: tuple[int, str]
    """(id, title) of the item that stays."""
    others: tuple[tuple[int, str], ...]
    """(id, title) of the items merged into it."""
    type_label: str
    tags: int
    """Tags the kept item gains."""
    files: int
    """Files moved to the kept item."""
    unplaced: tuple[str, ...]
    """Files left linked to nothing (a one-file role already filled), as "root, then path"."""
    containers: int
    """Items (albums, folders…) that will contain the kept item, new to it."""
    contents: int
    """Items the kept item will contain, new to it."""
    related: int
    """Relationships the kept item gains."""
    conflicts: tuple[Conflict, ...]


@dataclass(frozen=True)
class MergeChange:
    """Everything needed to undo and redo one merge."""

    label: str
    action: ActionChange
    merges: tuple[tuple[int, str, str | None, int], ...]
    """``entity_merge`` rows written: (merged id, type, ingest key, into id)."""
    files: tuple[tuple[int, int, str], ...] = ()
    """``entity_merge_resource`` rows written: (merged id, resource id, role)."""
    repointed: tuple[tuple[int, int], ...] = ()
    """Earlier merges into the items merged now, as (merged id, the item it went into):
    they now point at the kept item, so every record names a living item in one step."""


def plan_merge(
    conn: Connection, schema: ThemeSchema, keep_id: int, other_ids: Sequence[int]
) -> MergePlan:
    """What merging ``other_ids`` into ``keep_id`` would do. Raises :class:`MergeError`
    when it can't be done (an item is gone, or the types differ)."""
    keep, others, table = _items(conn, schema, keep_id, other_ids)
    ids = [o[0] for o in others]
    kept_tags = set(conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id == keep_id)))
    their_tags = set(conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id.in_(ids))))
    moves, unplaced = _file_moves(conn, table, keep_id, ids)
    parents, children = _edges(conn, keep_id, ids)
    have_parents = set(
        conn.scalars(select(EntityContains.parent_id).where(EntityContains.child_id == keep_id))
    )
    have_children = set(
        conn.scalars(select(EntityContains.child_id).where(EntityContains.parent_id == keep_id))
    )
    relations = _relations(conn, schema, keep_id, ids)
    return MergePlan(
        keep=keep,
        others=others,
        type_label=entity_plural(table.entity).lower(),
        tags=len(their_tags - kept_tags),
        files=len({r for r, _, _ in moves}),
        unplaced=tuple(_where(conn, unplaced)),
        containers=len(parents - have_parents),
        contents=len(children - have_children),
        related=len(relations),
        conflicts=tuple(_conflicts(conn, table, keep_id, ids)),
    )


def merge_items(
    conn: Connection,
    schema: ThemeSchema,
    keep_id: int,
    other_ids: Sequence[int],
    choices: Mapping[str, int] | None = None,
) -> MergeChange:
    """Merge ``other_ids`` into ``keep_id`` (see the module docstring). ``choices`` maps a
    :class:`Conflict` key to the entity id whose value wins; unlisted conflicts take their
    default."""
    keep, others, table = _items(conn, schema, keep_id, other_ids)
    ids = [o[0] for o in others]
    chosen = dict(choices or {})
    values = _winning_values(conn, table, keep_id, ids, chosen)
    moves, _ = _file_moves(conn, table, keep_id, ids)
    parents, children = _edges(conn, keep_id, ids)
    relations = _relations(conn, schema, keep_id, ids)
    their_tags = list(
        conn.execute(
            select(EntityTag.tag_id, EntityTag.added_at).where(EntityTag.entity_id.in_(ids))
        )
    )
    keys = {
        row.id: row
        for row in conn.execute(
            select(Entity.id, Entity.type, Entity.ingest_key).where(Entity.id.in_(ids))
        )
    }

    files = tuple(
        conn.execute(
            select(EntityResource.entity_id, EntityResource.resource_id, EntityResource.role).where(
                EntityResource.entity_id.in_(ids), EntityResource.by_user.is_(False)
            )
        ).all()
    )
    their_records = list(
        conn.execute(
            select(
                UserRelation.name, UserRelation.a_id, UserRelation.b_id, UserRelation.added
            ).where(or_(UserRelation.a_id.in_(ids), UserRelation.b_id.in_(ids)))
        )
    )
    their_orders = set(conn.scalars(select(UserOrder.name).where(UserOrder.b_id.in_(ids))))
    recorder = ChangeRecorder(conn, schema)
    recorder.touch([keep_id, *ids, *parents, *children, *_partners(conn, schema, ids)])

    # The others go first (their links, tags, and edges with them), then the kept item
    # takes what was theirs.
    closure.detach(conn, ids)
    for link in schema.relationships.values():
        t = link.table
        conn.execute(delete(t).where(or_(t.c.a_id.in_(ids), t.c.b_id.in_(ids))))
    conn.execute(delete(Entity).where(Entity.id.in_(ids)))
    merges = tuple((i, keys[i].type, keys[i].ingest_key, keep_id) for i in ids)
    repointed = tuple(
        conn.execute(
            select(EntityMerge.merged_id, EntityMerge.into_id).where(EntityMerge.into_id.in_(ids))
        ).all()
    )
    conn.execute(update(EntityMerge).where(EntityMerge.into_id.in_(ids)).values(into_id=keep_id))
    _write_merges(conn, merges, files)

    if their_tags:
        conn.execute(
            insert(EntityTag).prefix_with("OR IGNORE"),
            [{"entity_id": keep_id, "tag_id": t, "added_at": a} for t, a in their_tags],
        )
    for resource_id, role, sort_order in moves:
        conn.execute(
            sqlite_insert(EntityResource)
            .values(
                entity_id=keep_id,
                resource_id=resource_id,
                role=role,
                sort_order=sort_order,
                by_user=True,
            )
            .on_conflict_do_nothing()
        )
    closure.apply(
        conn,
        added=sorted({(p, keep_id) for p in parents} | {(keep_id, c) for c in children}),
        removed=[],
    )
    for name, rows in relations.items():
        link = schema.relationships[name]
        if not link.relationship.ordered:
            conn.execute(
                insert(link.table).prefix_with("OR IGNORE"),
                [{"a_id": a, "b_id": b} for a, b, _, _ in rows],
            )
            continue
        # An item merged on the a side keeps its place; a merged b's items go last (#317).
        for a, b, position, moved in rows:
            at = next_position(conn, link.table, b) if moved else position
            conn.execute(
                insert(link.table).prefix_with("OR IGNORE").values(a_id=a, b_id=b, position=at)
            )
    if their_orders:  # the user's hand-made orders come along
        conn.execute(
            insert(UserOrder).prefix_with("OR IGNORE"),
            [{"name": n, "b_id": keep_id} for n in sorted(their_orders)],
        )
    gone = set(ids)
    records = {
        (n, keep_id if a in gone else a, keep_id if b in gone else b, added)
        for n, a, b, added in their_records
    }
    records = {r for r in records if r[1] != r[2]}
    if records:  # the user's hand-made and hand-removed relationships come along
        conn.execute(
            insert(UserRelation).prefix_with("OR IGNORE"),
            [{"name": n, "a_id": a, "b_id": b, "added": added} for n, a, b, added in records],
        )
    _write_values(conn, table, keep_id, values)
    conn.execute(
        update(Entity)
        .where(Entity.id == keep_id)
        .values(thumb_resource_id=None, updated_at=utcnow())
    )
    fts.sync_entities(conn, [keep_id, *ids], theme_text_source(schema))

    count = len(ids)
    what = f"{others[0][1]!r}" if count == 1 else f"{count} items"
    label = f"Merge {what} into {keep[1]!r}"
    action = ActionChange(label, recorder.before, recorder.after())
    return MergeChange(
        label,
        action,
        merges,
        repointed=tuple((m, i) for m, i in repointed),
        files=tuple((m, r, role) for m, r, role in files),
    )


def restore_merge(
    conn: Connection, schema: ThemeSchema, change: MergeChange, *, forward: bool
) -> None:
    """Undo a merge (the items come back as they were, and are no longer remembered as
    merged) or redo it."""
    ids = [m[0] for m in change.merges]
    conn.execute(delete(EntityMerge).where(EntityMerge.merged_id.in_(ids)))
    restore_states(conn, schema, change.action.after if forward else change.action.before)
    keep = change.merges[0][3] if change.merges else None
    for merged_id, into_id in change.repointed:
        conn.execute(
            update(EntityMerge)
            .where(EntityMerge.merged_id == merged_id)
            .values(into_id=keep if forward else into_id)
        )
    if forward:
        _write_merges(conn, change.merges, change.files)


# --- reading what each item has ---


def _items(
    conn: Connection, schema: ThemeSchema, keep_id: int, other_ids: Sequence[int]
) -> tuple[tuple[int, str], tuple[tuple[int, str], ...], EntityTable]:
    wanted = [i for i in dict.fromkeys(other_ids) if i != keep_id]
    if not wanted:
        raise MergeError("Choose at least two items to merge.")
    rows = {
        row.id: row
        for row in conn.execute(
            select(Entity.id, Entity.type, Entity.title).where(Entity.id.in_([keep_id, *wanted]))
        )
    }
    if len(rows) != len(wanted) + 1:
        raise MergeError("An item to merge no longer exists; press F5.")
    types = {row.type for row in rows.values()}
    if len(types) > 1:
        raise MergeError("Only items of the same type can be merged.")
    try:
        table = schema.by_type_id(rows[keep_id].type)
    except KeyError:
        raise MergeError("These items' type isn't in the theme.") from None
    keep = (keep_id, rows[keep_id].title)
    return keep, tuple((i, rows[i].title) for i in wanted), table


def _file_moves(
    conn: Connection, table: EntityTable, keep_id: int, ids: list[int]
) -> tuple[list[tuple[int, str, int]], list[int]]:
    """(resource, role, sort order) links the kept item gains, and files left over."""
    roles = {r.name: r for r in table.entity.roles}
    current = conn.execute(
        select(EntityResource.resource_id, EntityResource.role).where(
            EntityResource.entity_id == keep_id
        )
    ).all()
    linked = {(r, role) for r, role in current}
    filled = {role for _, role in current}
    last = dict(
        conn.execute(
            select(EntityResource.role, func.max(EntityResource.sort_order))
            .where(EntityResource.entity_id == keep_id)
            .group_by(EntityResource.role)
        ).all()
    )
    moves: list[tuple[int, str, int]] = []
    unplaced: list[int] = []
    rows = conn.execute(
        select(EntityResource.resource_id, EntityResource.role)
        .where(EntityResource.entity_id.in_(ids))
        .order_by(EntityResource.entity_id, EntityResource.sort_order, EntityResource.resource_id)
    ).all()
    for resource_id, role in rows:
        if (resource_id, role) in linked:
            continue
        declared = roles.get(role)
        if declared is not None and not declared.many and role in filled:
            unplaced.append(resource_id)
            continue
        order = last.get(role)
        order = 0 if order is None else order + 1
        last[role] = order
        moves.append((resource_id, role, order))
        linked.add((resource_id, role))
        filled.add(role)
    # A file left over in one role but moved in another isn't left linked to nothing.
    moved = {r for r, _, _ in moves}
    return moves, list(dict.fromkeys(r for r in unplaced if r not in moved))


def _edges(conn: Connection, keep_id: int, ids: list[int]) -> tuple[set[int], set[int]]:
    """The others' containers and contents (other than the kept item and each other)."""
    skip = {keep_id, *ids}
    parents = set(
        conn.scalars(select(EntityContains.parent_id).where(EntityContains.child_id.in_(ids)))
    )
    children = set(
        conn.scalars(select(EntityContains.child_id).where(EntityContains.parent_id.in_(ids)))
    )
    return parents - skip, children - skip


def _relations(
    conn: Connection, schema: ThemeSchema, keep_id: int, ids: list[int]
) -> dict[str, list[tuple[int, int, int | None, bool]]]:
    """Relationship rows the kept item gains, by relationship name: ``(a, b, position,
    whether b changed)``, in position order for ordered relationships."""
    gone = set(ids)
    found: dict[str, list[tuple[int, int, int | None, bool]]] = {}
    for name, link in schema.relationships.items():
        t = link.table
        position = t.c.position if link.relationship.ordered else literal(None)
        current = set(
            conn.execute(
                select(t.c.a_id, t.c.b_id).where(or_(t.c.a_id == keep_id, t.c.b_id == keep_id))
            )
        )
        has_partner = any(b == keep_id for _, b in current)
        rows: list[tuple[int, int, int | None, bool]] = []
        for a, b, at in conn.execute(
            select(t.c.a_id, t.c.b_id, position)
            .where(or_(t.c.a_id.in_(ids), t.c.b_id.in_(ids)))
            .order_by(func.coalesce(position, LAST_POSITION), t.c.a_id)
        ):
            a2 = keep_id if a in gone else a
            b2 = keep_id if b in gone else b
            if a2 == b2 or (a2, b2) in current or any((r[0], r[1]) == (a2, b2) for r in rows):
                continue
            if b2 == keep_id and not link.relationship.many:
                if has_partner:
                    continue  # the kept item keeps its one partner
                has_partner = True
            rows.append((a2, b2, at, b2 != b))
        if rows:
            found[name] = rows
    return found


def _partners(conn: Connection, schema: ThemeSchema, ids: list[int]) -> set[int]:
    found: set[int] = set()
    for link in schema.relationships.values():
        t = link.table
        found |= set(conn.scalars(select(t.c.b_id).where(t.c.a_id.in_(ids))))
        found |= set(conn.scalars(select(t.c.a_id).where(t.c.b_id.in_(ids))))
    return found - set(ids)


def _where(conn: Connection, resource_ids: list[int]) -> list[str]:
    if not resource_ids:
        return []
    rows = conn.execute(
        select(Root.name, Resource.relpath)
        .join(Root, Root.id == Resource.root_id)
        .where(Resource.id.in_(resource_ids))
        .order_by(Root.name, Resource.relpath)
    )
    return [f"{name} {PATH_MARK} {path}" if path else name for name, path in rows]


# --- values set by hand ---

Value = tuple[Any, bool]
"""A value and whether the user set it by hand."""


def _hand_values(conn: Connection, table: EntityTable, ids: list[int]) -> dict[str, dict[int, Any]]:
    """For each key (``title``, ``field:x``, ``extra:x``), the hand-set value of each item
    that has one."""
    edited: dict[int, set[str]] = {}
    for entity_id, name in conn.execute(
        select(FieldProvenance.entity_id, FieldProvenance.field).where(
            FieldProvenance.entity_id.in_(ids), FieldProvenance.source == FieldSource.USER
        )
    ):
        edited.setdefault(entity_id, set()).add(name)
    fields = {f.name for f in table.fields}
    found: dict[str, dict[int, Any]] = {}
    rows = conn.execute(
        select(Entity.id, Entity.title, Entity.extra, table.table)
        .join(table.table, table.table.c.id == Entity.id)
        .where(Entity.id.in_(ids))
    ).all()
    by_id = {row.id: row for row in rows}
    for entity_id in ids:  # in order: the kept item first
        row = by_id.get(entity_id)
        if row is None:
            continue
        values = row._mapping
        for name in sorted(edited.get(entity_id, ())):
            if name == TITLE:
                found.setdefault("title", {})[entity_id] = row.title
            elif name in fields:
                found.setdefault(f"field:{name}", {})[entity_id] = values[table.table.c[name]]
        for name, value in (row.extra or {}).items():
            found.setdefault(f"extra:{name}", {})[entity_id] = value
    return found


def _conflicts(
    conn: Connection, table: EntityTable, keep_id: int, ids: list[int]
) -> list[Conflict]:
    labels = {f"field:{f.name}": f.spec.label for f in table.fields}
    found = []
    for key, values in _hand_values(conn, table, [keep_id, *ids]).items():
        if len({_text(v) for v in values.values()}) < 2:
            continue
        label = (
            table.entity.title_label if key == "title" else labels.get(key, key.partition(":")[2])
        )
        choices = tuple(Choice(i, _text(v)) for i, v in values.items())
        default = keep_id if keep_id in values else choices[0].entity_id
        found.append(Conflict(key, label, choices, default))
    return found


def _winning_values(
    conn: Connection,
    table: EntityTable,
    keep_id: int,
    ids: list[int],
    choices: Mapping[str, int],
) -> dict[str, Any]:
    """The hand-set values the kept item ends with, by key, where they come from another
    item (the kept item's own stay as they are)."""
    won: dict[str, Any] = {}
    for key, values in _hand_values(conn, table, [keep_id, *ids]).items():
        pick = choices.get(key)
        if pick not in values:
            pick = keep_id if keep_id in values else next(iter(values))
        if pick != keep_id:
            won[key] = values[pick]
    return won


def _write_values(
    conn: Connection, table: EntityTable, keep_id: int, values: Mapping[str, Any]
) -> None:
    extra_changes = {k.partition(":")[2]: v for k, v in values.items() if k.startswith("extra:")}
    if extra_changes:
        extra = dict(conn.scalar(select(Entity.extra).where(Entity.id == keep_id)) or {})
        extra.update(extra_changes)
        conn.execute(update(Entity).where(Entity.id == keep_id).values(extra=extra))
    for key, value in values.items():
        if key == "title":
            conn.execute(update(Entity).where(Entity.id == keep_id).values(title=value))
            name = TITLE
        elif key.startswith("field:"):
            name = key.partition(":")[2]
            conn.execute(
                update(table.table).where(table.table.c.id == keep_id).values({name: value})
            )
        else:
            continue
        stmt = sqlite_insert(FieldProvenance).values(
            entity_id=keep_id, field=name, source=FieldSource.USER, updated_at=utcnow()
        )
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["entity_id", "field"],
                set_={"source": stmt.excluded.source, "updated_at": stmt.excluded.updated_at},
            )
        )


def _write_merges(
    conn: Connection,
    merges: Sequence[tuple[int, str, str | None, int]],
    files: Sequence[tuple[int, int, str]] = (),
) -> None:
    if merges:
        conn.execute(
            insert(EntityMerge).prefix_with("OR REPLACE"),
            [
                {"merged_id": m, "type": t, "ingest_key": k, "into_id": i, "merged_at": utcnow()}
                for m, t, k, i in merges
            ],
        )
    if files:  # after the merge rows they refer to
        conn.execute(
            insert(EntityMergeResource).prefix_with("OR IGNORE"),
            [{"merged_id": m, "resource_id": r, "role": role} for m, r, role in files],
        )


def _text(value: Any) -> str:
    return "" if value is None else str(value)
