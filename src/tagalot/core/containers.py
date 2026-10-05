"""Containers made by hand (DESIGN.md §9, #329): a theme marks a type ``made_by_hand``
(the research theme's Project), and the user makes items of it and puts items in them.

Each change is one undo step, an :class:`~tagalot.core.actions.ActionChange` over the
items' snapshots (which carry their containment and the user's record of it). The record,
in ``user_contains``, is what scans respect: a theme's ``uncontain`` leaves an edge the user
added, and its ``contain`` doesn't bring back one the user removed.
"""

from collections.abc import Sequence

from sqlalchemy import Connection, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core import closure
from tagalot.core.actions import ActionChange
from tagalot.core.entity_state import ChangeRecorder
from tagalot.core.models import Entity, EntityContains, UserContains, utcnow
from tagalot.core.relations import RelationError, make_item
from tagalot.core.theme_schema import ThemeSchema


class ContainerError(RelationError):
    """A container that can't be made or filled; the message is for the user."""


def hand_made_types(schema: ThemeSchema) -> list[str]:
    """The type ids of the theme's types the user makes by hand (``made_by_hand``)."""
    theme = schema.theme
    return [theme.type_id_of(e) for e in theme.entities if getattr(e, "made_by_hand", False)]


def containers_for(schema: ThemeSchema, type_id: str) -> list[str]:
    """The hand-made types that can hold items of ``type_id`` (a paper: Project)."""
    theme = schema.theme
    made = set(hand_made_types(schema))
    return [
        theme.type_id_of(c.parent)
        for c in theme.containment
        if theme.type_id_of(c.child) == type_id and theme.type_id_of(c.parent) in made
    ]


def new_container(
    conn: Connection, schema: ThemeSchema, type_id: str, title: str
) -> tuple[ActionChange, int]:
    """Make an item of a hand-made type, named ``title``; returns the change and its id."""
    if type_id not in hand_made_types(schema):
        raise ContainerError("Items of that type come from files, not by hand.")
    title = " ".join(title.split())
    if not title:
        raise ContainerError("Type a name for it.")
    recorder = ChangeRecorder(conn, schema)
    entity_id = make_item(conn, schema, type_id, title)
    recorder.created(entity_id)
    label = schema.by_type_id(type_id).entity
    noun = (getattr(label, "label", None) or label.__name__).lower()
    return ActionChange(f"New {noun} {title!r}", recorder.before, recorder.after()), entity_id


def contain_by_hand(
    conn: Connection, schema: ThemeSchema, parent_id: int, child_ids: Sequence[int]
) -> ActionChange:
    """Put items in a container by hand (remembered, so scans keep them there). Items
    already in it are left alone. One undo step."""
    parent_type, parent_title = _item(conn, parent_id)
    children = [c for c in dict.fromkeys(child_ids) if c != parent_id]
    allowed = _children_of(schema, parent_type)
    for child in children:
        child_type, child_title = _item(conn, child)
        if child_type not in allowed:
            raise ContainerError(f"{child_title} can't go in {parent_title}.")
    recorder = ChangeRecorder(conn, schema)
    recorder.touch([parent_id, *children])
    result = closure.apply(conn, added=[(parent_id, c) for c in children])
    if result.rejected:
        (_, child), reason = result.rejected[0]
        raise ContainerError(f"{_item(conn, child)[1]} can't go in {parent_title}: {reason}.")
    for child in children:
        _record(conn, parent_id, child, added=True)
    what = repr(_item(conn, children[0])[1]) if len(children) == 1 else f"{len(children)} items"
    return ActionChange(f"Add {what} to {parent_title}", recorder.before, recorder.after())


def uncontain_by_hand(
    conn: Connection, schema: ThemeSchema, parent_id: int, child_ids: Sequence[int]
) -> ActionChange:
    """Take items out of a container by hand (remembered, so scans don't put them back).
    One undo step."""
    _, parent_title = _item(conn, parent_id)
    children = list(dict.fromkeys(child_ids))
    inside = set(
        conn.scalars(
            select(EntityContains.child_id).where(
                EntityContains.parent_id == parent_id, EntityContains.child_id.in_(children)
            )
        )
    )
    missing = [c for c in children if c not in inside]
    if missing:
        raise ContainerError(f"{_item(conn, missing[0])[1]} isn't in {parent_title}.")
    recorder = ChangeRecorder(conn, schema)
    recorder.touch([parent_id, *children])
    closure.apply(conn, removed=[(parent_id, c) for c in children])
    for child in children:
        _record(conn, parent_id, child, added=False)
    what = repr(_item(conn, children[0])[1]) if len(children) == 1 else f"{len(children)} items"
    return ActionChange(f"Remove {what} from {parent_title}", recorder.before, recorder.after())


def _children_of(schema: ThemeSchema, parent_type: str) -> set[str]:
    theme = schema.theme
    return {
        theme.type_id_of(c.child)
        for c in theme.containment
        if theme.type_id_of(c.parent) == parent_type
    }


def _record(conn: Connection, parent_id: int, child_id: int, *, added: bool) -> None:
    stmt = sqlite_insert(UserContains).values(
        parent_id=parent_id, child_id=child_id, added=added, at=utcnow()
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["parent_id", "child_id"],
            set_={"added": stmt.excluded.added, "at": stmt.excluded.at},
        )
    )


def _item(conn: Connection, entity_id: int) -> tuple[str, str]:
    row = conn.execute(select(Entity.type, Entity.title).where(Entity.id == entity_id)).first()
    if row is None:
        raise ContainerError("That item no longer exists; press F5.")
    return row.type, row.title
