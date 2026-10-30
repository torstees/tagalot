"""Linking places (DESIGN.md §13 *Linked places*, #598): see :mod:`tagalot.core.places`.

:func:`link_place` and :func:`link_as_one` link places, and :func:`split_place` makes a
place a file of its own again (**Unlink**, #600); each returns a :class:`PlaceChange`, one
undo step (:func:`restore_place_change`). Linking folders (#601) is
:mod:`tagalot.core.linked_folders`, whose change also records the folders' pair.
"""

import posixpath
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from sqlalchemy import Connection, delete, insert, or_, select, tuple_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core import closure
from tagalot.core.actions import ActionChange, restore_action
from tagalot.core.entity_state import ChangeRecorder, EntitySnapshot, restore_states
from tagalot.core.ingest import next_entity_id
from tagalot.core.merge import MergeChange, merge_items, restore_merge
from tagalot.core.models import (
    Entity,
    EntityContains,
    EntityResource,
    LinkedFolder,
    LinkedPlace,
    PlaceContains,
    Resource,
    UserContains,
)
from tagalot.core.places import HOWS, first_place
from tagalot.core.theme_schema import ThemeSchema


class PlaceError(ValueError):
    """A link that can't be made; the message is for the user."""


@dataclass(frozen=True)
class PlaceRows:
    """The place tables' rows about some resources, as they were or became."""

    linked: frozenset[tuple[int, int, str]] = frozenset()
    """``linked_place``: (place, first place, how)."""
    contains: frozenset[tuple[int, int, int]] = frozenset()
    """``place_contains``: (place, parent, child)."""
    links: frozenset[tuple[int, int, str, int, bool, bool]] = frozenset()
    """``entity_resource`` rows of the resources: (entity, resource, role, sort order, by
    the user, by a gallery's folders)."""


FolderRow = tuple[str, str, str, str]
"""A ``linked_folder`` row: (root, path, first folder's root, its path)."""


@dataclass(frozen=True)
class PlaceChange:
    """Everything needed to undo and redo linking or unlinking places (#600)."""

    label: str
    resources: tuple[int, ...]
    """The resources whose place rows changed."""
    before: PlaceRows
    after: PlaceRows
    merges: tuple[MergeChange, ...] = ()
    """Items merged into their group's items, in order."""
    action: ActionChange | None = None
    """Items changed otherwise (the item **Unlink** makes)."""
    folders: tuple[tuple[str, str], ...] = ()
    """Linked folders (#601) whose ``linked_folder`` rows changed: (root, path)."""
    folders_before: frozenset[FolderRow] = frozenset()
    folders_after: frozenset[FolderRow] = frozenset()


def link_as_one(conn: Connection, schema: ThemeSchema, resource_ids: Sequence[int]) -> PlaceChange:
    """**Link as one** (#600): the files (or folders) after the first become places of the
    first's group, linked by the user; one undo step."""
    ids = list(dict.fromkeys(resource_ids))
    if len(ids) < 2:
        raise PlaceError("Choose two or more copies to link.")
    first = first_place(conn, ids[0])
    others = [i for i in ids[1:] if first_place(conn, i) != first]
    if not others:
        raise PlaceError("These are linked already.")
    touched = _touched(conn, [first, *others])
    before = _rows(conn, touched)
    merges: list[MergeChange] = []
    for place in others:
        merges.extend(_link(conn, schema, place, first, "user"))
    after = _rows(conn, touched)
    n = len(others) + 1
    return PlaceChange(f"Link {n} copies as one", tuple(touched), before, after, tuple(merges))


def link_place(
    conn: Connection, schema: ThemeSchema, place_id: int, first_id: int, how: str
) -> PlaceChange:
    """Link one place (see :func:`_link`); one undo step."""
    touched = _touched(conn, [first_id, place_id])
    before = _rows(conn, touched)
    merges = _link(conn, schema, place_id, first_id, how)
    after = _rows(conn, touched)
    return PlaceChange("Link a place", tuple(touched), before, after, tuple(merges))


def _link(
    conn: Connection, schema: ThemeSchema, place_id: int, first_id: int, how: str
) -> list[MergeChange]:
    """Link ``place_id`` as a place of ``first_id``'s group (its first place's, when
    ``first_id`` is itself a linked place). Places linked to ``place_id`` join the group
    too. An item the place was a file of is merged into the group's item of its type in
    that role, keeping the group's item (§13: its values win where both were set by hand);
    one with no such item just loses the file. The place is read again at the next scan,
    to file the group's items where its path says."""
    if how not in HOWS:
        raise ValueError(f"not a way of linking places: {how!r}")
    first = first_place(conn, first_id)
    if first == place_id:
        raise PlaceError("A file can't be linked to itself.")
    kinds = conn.execute(
        select(Resource.id, Resource.kind).where(Resource.id.in_([place_id, first]))
    ).all()
    if len(kinds) != 2:
        raise PlaceError("That file is no longer in the keep.")
    if kinds[0][1] != kinds[1][1]:
        raise PlaceError("A file can only be linked to a file, and a folder to a folder.")
    conn.execute(update(LinkedPlace).where(LinkedPlace.first_id == place_id).values(first_id=first))
    merges = _fold_items(conn, schema, place_id, first)
    conn.execute(
        sqlite_insert(LinkedPlace)
        .values(resource_id=place_id, first_id=first, how=how)
        .on_conflict_do_update(
            index_elements=[LinkedPlace.resource_id], set_={"first_id": first, "how": how}
        )
    )
    conn.execute(update(Resource).where(Resource.id == place_id).values(ingested_at=None))
    return merges


def _fold_items(
    conn: Connection, schema: ThemeSchema, place_id: int, first: int
) -> list[MergeChange]:
    """The place's items become the group's: each is merged into the item of its type
    linked to the first place in the same role, and the place stops being a file of any.
    The containers the group's item gains from a merged item are recorded as the place's,
    as the place's scans would have given them."""
    types = {t.type_id for t in schema.entities.values()}
    rows = conn.execute(
        select(EntityResource.entity_id, EntityResource.role, Entity.type)
        .join(Entity, Entity.id == EntityResource.entity_id)
        .where(EntityResource.resource_id == place_id)
        .order_by(EntityResource.entity_id, EntityResource.role)
    ).all()
    merged: set[int] = set()
    changes: list[MergeChange] = []
    for entity_id, role, type_id in rows:
        if entity_id in merged or type_id not in types:
            continue
        keep = conn.scalar(
            select(EntityResource.entity_id)
            .join(Entity, Entity.id == EntityResource.entity_id)
            .where(
                EntityResource.resource_id == first,
                EntityResource.role == role,
                Entity.type == type_id,
            )
            .order_by(EntityResource.by_user, EntityResource.entity_id)
            .limit(1)
        )
        if keep is not None and keep != entity_id:
            had = _parents(conn, keep)
            changes.append(merge_items(conn, schema, keep, [entity_id]))
            merged.add(entity_id)
            gained = _parents(conn, keep) - had
            if gained:  # the containers it came with are the place's (it gives them)
                conn.execute(
                    insert(PlaceContains).prefix_with("OR IGNORE"),
                    [
                        {"resource_id": place_id, "parent_id": p, "child_id": keep}
                        for p in sorted(gained)
                    ],
                )
    conn.execute(delete(EntityResource).where(EntityResource.resource_id == place_id))
    return changes


def _parents(conn: Connection, entity_id: int) -> set[int]:
    return set(
        conn.scalars(select(EntityContains.parent_id).where(EntityContains.child_id == entity_id))
    )


def unlink_place(conn: Connection, place_id: int) -> None:
    """``place_id`` is no longer a place of its group: the containers it filed the group's
    items in go (unless another place gives them, or the user put them there), and it is
    read again at the next scan as a file of its own."""
    edges = [
        (p, c)
        for p, c in conn.execute(
            select(PlaceContains.parent_id, PlaceContains.child_id).where(
                PlaceContains.resource_id == place_id
            )
        )
    ]
    conn.execute(delete(PlaceContains).where(PlaceContains.resource_id == place_id))
    if not edges:
        still: set[tuple[int, int]] = set()
        by_hand: set[tuple[int, int]] = set()
    else:
        still = {
            (p, c)
            for p, c in conn.execute(
                select(PlaceContains.parent_id, PlaceContains.child_id).where(
                    tuple_(PlaceContains.parent_id, PlaceContains.child_id).in_(edges)
                )
            )
        }
        by_hand = {
            (p, c)
            for p, c in conn.execute(
                select(UserContains.parent_id, UserContains.child_id).where(
                    UserContains.added.is_(True),
                    tuple_(UserContains.parent_id, UserContains.child_id).in_(edges),
                )
            )
        }
    gone = [e for e in edges if e not in still and e not in by_hand]
    if gone:
        closure.apply(conn, removed=gone)
    conn.execute(delete(LinkedPlace).where(LinkedPlace.resource_id == place_id))
    conn.execute(update(Resource).where(Resource.id == place_id).values(ingested_at=None))


def split_place(conn: Connection, schema: ThemeSchema, place_id: int) -> PlaceChange:
    """**Unlink** (#600): a linked place becomes a file of its own, with an item of its own
    for each of the group's items: a copy of it (its title, values, the user's values, its
    tags, and a note typed in the app), filed in the containers the place gave, linked to
    the place in the same role. Scans then read the place as any file. One undo step."""
    row = conn.execute(
        select(LinkedPlace.first_id).where(LinkedPlace.resource_id == place_id)
    ).first()
    if row is None:
        raise PlaceError("That file isn't linked to another.")
    first = row[0]
    types = {t.type_id for t in schema.entities.values()}
    group: dict[int, tuple[str, int]] = {}
    for entity_id, role, order, type_id in conn.execute(
        select(
            EntityResource.entity_id, EntityResource.role, EntityResource.sort_order, Entity.type
        )
        .join(Entity, Entity.id == EntityResource.entity_id)
        .where(EntityResource.resource_id == first)
        .order_by(EntityResource.entity_id, EntityResource.role)
    ):
        if type_id in types:
            group.setdefault(entity_id, (role, order))
    given = [
        (p, c)
        for p, c in conn.execute(
            select(PlaceContains.parent_id, PlaceContains.child_id).where(
                PlaceContains.resource_id == place_id
            )
        )
    ]
    touched = [place_id]
    before = _rows(conn, touched)
    recorder = ChangeRecorder(conn, schema)
    recorder.touch(group)
    copies: dict[int, EntitySnapshot | None] = {}
    next_id = next_entity_id(conn)
    for entity_id, (role, order) in group.items():
        state = recorder.before[entity_id]
        if state is None:
            continue
        parents = frozenset(p for p, c in given if c == entity_id)
        copies[next_id] = _copy(schema, state, entity_id, next_id, place_id, role, order, parents)
        recorder.created(next_id)
        next_id += 1
    unlink_place(conn, place_id)
    restore_states(conn, schema, copies)
    relpath = conn.scalar(select(Resource.relpath).where(Resource.id == place_id)) or ""
    label = f"Unlink {posixpath.basename(relpath) or 'a place'}"
    action = ActionChange(label, recorder.before, recorder.after())
    return PlaceChange(label, tuple(touched), before, _rows(conn, touched), action=action)


def _copy(
    schema: ThemeSchema,
    state: EntitySnapshot,
    old_id: int,
    new_id: int,
    place_id: int,
    role: str,
    order: int,
    parents: frozenset[int],
) -> EntitySnapshot:
    """An item of its own for an unlinked place, from its group's item."""

    def mine(i: int) -> int:
        return new_id if i == old_id else i

    relations = frozenset(
        (name, mine(a), mine(b), position)
        for name, a, b, position in state.relations
        # A one-partner relationship's other item keeps its partner.
        if name in schema.relationships
        and (schema.relationships[name].relationship.many or b == old_id)
    )
    note = state.note
    typed = note is not None and note[0] is None and note[1] is None
    return replace(
        state,
        ingest_key=None,
        links=frozenset({(place_id, role, order, False, False)}),
        parents=parents,
        children=frozenset(),
        relations=relations,
        user_relations=frozenset(),
        user_contains=frozenset(),
        user_orders=frozenset(),
        note=note if typed else None,
        note_removals=state.note_removals if typed else frozenset(),
    )


def restore_place_change(
    conn: Connection, schema: ThemeSchema, change: PlaceChange, *, forward: bool
) -> None:
    """Undo linking or unlinking places, or redo it."""
    if forward:
        for merge in change.merges:
            restore_merge(conn, schema, merge, forward=True)
        if change.action is not None:
            restore_action(conn, schema, change.action, forward=True)
        _set_rows(conn, change.resources, change.after)
        _set_folders(conn, change.folders, change.folders_after)
    else:
        if change.action is not None:
            restore_action(conn, schema, change.action, forward=False)
        for merge in reversed(change.merges):
            restore_merge(conn, schema, merge, forward=False)
        _set_rows(conn, change.resources, change.before)
        _set_folders(conn, change.folders, change.folders_before)
    # Read again at the next scan, as they are now.
    conn.execute(update(Resource).where(Resource.id.in_(change.resources)).values(ingested_at=None))


def folder_rows(conn: Connection, folders: Iterable[tuple[str, str]]) -> frozenset[FolderRow]:
    """The ``linked_folder`` rows of these folders, (root, path)."""
    wanted = list(folders)
    if not wanted:
        return frozenset()
    rows = conn.execute(
        select(
            LinkedFolder.root_id,
            LinkedFolder.relpath,
            LinkedFolder.first_root_id,
            LinkedFolder.first_relpath,
        ).where(tuple_(LinkedFolder.root_id, LinkedFolder.relpath).in_(wanted))
    )
    return frozenset((a, b, c, d) for a, b, c, d in rows)


def _set_folders(
    conn: Connection, folders: Sequence[tuple[str, str]], rows: frozenset[FolderRow]
) -> None:
    if not folders:
        return
    conn.execute(
        delete(LinkedFolder).where(tuple_(LinkedFolder.root_id, LinkedFolder.relpath).in_(folders))
    )
    if rows:
        conn.execute(
            insert(LinkedFolder),
            [
                {"root_id": a, "relpath": b, "first_root_id": c, "first_relpath": d}
                for a, b, c, d in sorted(rows)
            ],
        )


def _touched(conn: Connection, ids: Sequence[int]) -> list[int]:
    """These resources and the places linked to them: whose place rows a change may
    change."""
    found = list(dict.fromkeys(ids))
    found += [
        i
        for i in conn.scalars(
            select(LinkedPlace.resource_id).where(LinkedPlace.first_id.in_(found))
        )
        if i not in found
    ]
    return found


def _rows(conn: Connection, resources: Sequence[int]) -> PlaceRows:
    ids = list(resources)
    linked = conn.execute(
        select(LinkedPlace.resource_id, LinkedPlace.first_id, LinkedPlace.how).where(
            LinkedPlace.resource_id.in_(ids)
        )
    )
    contains = conn.execute(
        select(PlaceContains.resource_id, PlaceContains.parent_id, PlaceContains.child_id).where(
            PlaceContains.resource_id.in_(ids)
        )
    )
    links = conn.execute(
        select(
            EntityResource.entity_id,
            EntityResource.resource_id,
            EntityResource.role,
            EntityResource.sort_order,
            EntityResource.by_user,
            EntityResource.by_folder,
        ).where(EntityResource.resource_id.in_(ids))
    )
    return PlaceRows(
        frozenset((a, b, c) for a, b, c in linked),
        frozenset((a, b, c) for a, b, c in contains),
        frozenset((a, b, c, d, bool(e), bool(f)) for a, b, c, d, e, f in links),
    )


def _set_rows(conn: Connection, resources: Sequence[int], rows: PlaceRows) -> None:
    """Make the place tables' rows about ``resources`` exactly ``rows`` (those naming items
    that exist)."""
    ids = list(resources)
    conn.execute(delete(LinkedPlace).where(LinkedPlace.resource_id.in_(ids)))
    conn.execute(delete(PlaceContains).where(PlaceContains.resource_id.in_(ids)))
    conn.execute(delete(EntityResource).where(EntityResource.resource_id.in_(ids)))
    if rows.linked:
        conn.execute(
            insert(LinkedPlace),
            [{"resource_id": a, "first_id": b, "how": c} for a, b, c in sorted(rows.linked)],
        )
    named = {c[1] for c in rows.contains} | {c[2] for c in rows.contains}
    named |= {link[0] for link in rows.links}
    alive = set(conn.scalars(select(Entity.id).where(or_(Entity.id.in_(named)))))
    contains = [c for c in sorted(rows.contains) if c[1] in alive and c[2] in alive]
    if contains:
        conn.execute(
            insert(PlaceContains),
            [{"resource_id": a, "parent_id": b, "child_id": c} for a, b, c in contains],
        )
    links = [link for link in sorted(rows.links) if link[0] in alive]
    if links:
        conn.execute(
            insert(EntityResource),
            [
                {
                    "entity_id": e,
                    "resource_id": r,
                    "role": role,
                    "sort_order": order,
                    "by_user": by_user,
                    "by_folder": by_folder,
                }
                for e, r, role, order, by_user, by_folder in links
            ],
        )
