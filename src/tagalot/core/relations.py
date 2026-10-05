"""Relating items by hand: adding someone to a cast, or removing them (DESIGN.md §9, §12;
#260).

Each is one undo step, an :class:`~tagalot.core.actions.ActionChange` over both items'
snapshots (which carry the relationship and the user's record of it). The record, in
``user_relation``, is what scans respect: a theme's ``unrelate`` leaves a relationship the
user added, and its ``relate`` doesn't bring back one the user removed.

In an ordered relationship (#317), an item added by hand goes last, and
:func:`move_related` reorders a ``b``'s items; ``user_order`` then records that the user
ordered them, so scans leave that order alone.
"""

from collections.abc import Sequence

from sqlalchemy import Connection, delete, func, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core import closure, fts
from tagalot.core.actions import ActionChange
from tagalot.core.entity_state import ChangeRecorder
from tagalot.core.ingest import next_entity_id, next_position, theme_text_source
from tagalot.core.models import LAST_POSITION, Entity, UserOrder, UserRelation, utcnow
from tagalot.core.theme_schema import RelationshipTable, ThemeSchema


class RelationError(ValueError):
    """A relationship that can't be made or removed; the message is for the user."""


def add_related(
    conn: Connection,
    schema: ThemeSchema,
    name: str,
    entity_id: int,
    other_id: int | None = None,
    *,
    new_title: str | None = None,
    side: str | None = None,
) -> ActionChange:
    """Relate ``entity_id`` and ``other_id`` through relationship ``name`` (either may be
    the ``a`` side), or, with ``new_title``, a new item of the other side's type made for
    it (an item the user made: no ingest key, so scans never delete it)."""
    link = _link(schema, name)
    rel = link.relationship
    entity_type, title = _item(conn, entity_id)
    a_type, b_type = (schema.theme.type_id_of(rel.a), schema.theme.type_id_of(rel.b))
    if a_type == b_type and side in ("a", "b"):  # a type with itself: cites, cited by
        other_type, this_is_a = a_type, side == "a"
    elif entity_type == a_type:
        other_type, this_is_a = b_type, True
    elif entity_type == b_type:
        other_type, this_is_a = a_type, False
    else:
        raise RelationError(f"{title} isn't part of {name!r}.")
    recorder = ChangeRecorder(conn, schema)
    recorder.touch([entity_id])
    if new_title is not None:
        new_title = " ".join(new_title.split())
        if not new_title:
            raise RelationError("Type a name for the new item.")
        other_id = _new_item(conn, schema, other_type, new_title)
        recorder.created(other_id)
        other_title = new_title
    else:
        if other_id is None:
            raise RelationError("Choose an item to add.")
        found_type, other_title = _item(conn, other_id)
        if found_type != other_type or other_id == entity_id:
            raise RelationError(f"{other_title} can't be added there.")
        recorder.touch([other_id])
    a_id, b_id = (entity_id, other_id) if this_is_a else (other_id, entity_id)
    t = link.table
    if not rel.many:  # b's one partner is replaced: the old one is touched too
        recorder.touch(conn.scalars(select(t.c.a_id).where(t.c.b_id == b_id)))
        conn.execute(delete(t).where(t.c.b_id == b_id, t.c.a_id != a_id))
    values = {"a_id": a_id, "b_id": b_id}
    if rel.ordered:  # last among b's items
        values["position"] = next_position(conn, t, b_id)
    conn.execute(insert(t).values(**values).prefix_with("OR IGNORE"))
    _record(conn, name, a_id, b_id, added=True)
    label = (rel.label if this_is_a else rel.reverse_label) or name
    return ActionChange(
        f"Add {other_title!r} to {title}'s {label.lower()}",
        recorder.before,
        recorder.after(),
    )


def remove_related(
    conn: Connection,
    schema: ThemeSchema,
    name: str,
    entity_id: int,
    other_ids: Sequence[int],
    side: str | None = None,
) -> ActionChange:
    """Unrelate these items from ``entity_id`` (on either side), remembering it, so a
    theme reading its files again doesn't relate them anew. One undo step for all."""
    link = _link(schema, name)
    _, title = _item(conn, entity_id)
    others = list(dict.fromkeys(other_ids))
    if not others:
        raise RelationError("Choose an item to remove.")
    t = link.table
    found: list[tuple[int, int]] = []
    titles: list[str] = []
    for other_id in others:
        _, other_title = _item(conn, other_id)
        pairs = {
            "a": ((entity_id, other_id),),  # the items this one relates to (it cites)
            "b": ((other_id, entity_id),),  # those relating to it (citing it)
        }.get(side or "", ((entity_id, other_id), (other_id, entity_id)))
        pair = next(
            (
                (a, b)
                for a, b in pairs
                if conn.scalar(select(t.c.a_id).where(t.c.a_id == a, t.c.b_id == b)) is not None
            ),
            None,
        )
        if pair is None:
            raise RelationError(f"{other_title} isn't related to {title} there.")
        found.append(pair)
        titles.append(other_title)
    recorder = ChangeRecorder(conn, schema)
    recorder.touch([entity_id, *others])
    for a_id, b_id in found:
        conn.execute(delete(t).where(t.c.a_id == a_id, t.c.b_id == b_id))
        _record(conn, name, a_id, b_id, added=False)
    rel = link.relationship
    this_is_a = found[0][0] == entity_id
    label = (rel.label if this_is_a else rel.reverse_label) or name
    what = repr(titles[0]) if len(titles) == 1 else f"{len(titles)} items"
    return ActionChange(
        f"Remove {what} from {title}'s {label.lower()}",
        recorder.before,
        recorder.after(),
    )


def move_related(
    conn: Connection,
    schema: ThemeSchema,
    name: str,
    b_id: int,
    a_ids: Sequence[int],
    by: int,
) -> ActionChange:
    """Move ``a_ids`` among ``b_id``'s items of ordered relationship ``name`` by ``by``
    places (-1: up one, 1: down one), keeping their order among themselves; they stop at
    the ends. Remembered (``user_order``), so scans leave this order alone. One undo
    step."""
    link = _link(schema, name)
    rel = link.relationship
    if not rel.ordered:
        raise RelationError(f"{name!r} has no order to change.")
    _, title = _item(conn, b_id)
    t = link.table
    order: list[int] = list(
        conn.scalars(
            select(t.c.a_id)
            .join(Entity, Entity.id == t.c.a_id)
            .where(t.c.b_id == b_id)
            .order_by(func.coalesce(t.c.position, LAST_POSITION), Entity.title, t.c.a_id)
        )
    )
    moving = [a for a in order if a in set(a_ids)]
    if not moving:
        raise RelationError(f"Those items aren't in {title}'s list.")
    new = _moved(order, moving, by)
    recorder = ChangeRecorder(conn, schema)
    recorder.touch([b_id])
    for position, a_id in enumerate(new):
        conn.execute(update(t).where(t.c.a_id == a_id, t.c.b_id == b_id).values(position=position))
    conn.execute(
        sqlite_insert(UserOrder)
        .values(name=name, b_id=b_id, at=utcnow())
        .on_conflict_do_update(index_elements=["name", "b_id"], set_={"at": utcnow()})
    )
    label = (rel.reverse_label or name).lower()
    what = "up" if by < 0 else "down"
    return ActionChange(f"Move {what} in {title}'s {label}", recorder.before, recorder.after())


def _moved(order: list[int], moving: list[int], by: int) -> list[int]:
    """``order`` with the ``moving`` items shifted ``by`` places, one step at a time, each
    stopping at an end or behind another moving item."""
    items = list(order)
    chosen = set(moving)
    for _ in range(abs(by)):
        step = -1 if by < 0 else 1
        indexes = range(len(items)) if step < 0 else range(len(items) - 1, -1, -1)
        for i in indexes:
            j = i + step
            if items[i] in chosen and 0 <= j < len(items) and items[j] not in chosen:
                items[i], items[j] = items[j], items[i]
    return items


def _record(conn: Connection, name: str, a_id: int, b_id: int, *, added: bool) -> None:
    stmt = sqlite_insert(UserRelation).values(
        name=name, a_id=a_id, b_id=b_id, added=added, at=utcnow()
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["name", "a_id", "b_id"],
            set_={"added": stmt.excluded.added, "at": stmt.excluded.at},
        )
    )


def _new_item(conn: Connection, schema: ThemeSchema, type_id: str, title: str) -> int:
    table = schema.by_type_id(type_id)
    entity_id = int(
        conn.execute(
            insert(Entity)
            .values(id=next_entity_id(conn), type=type_id, title=title, ingest_key=None)
            .returning(Entity.id)
        ).scalar_one()
    )
    conn.execute(insert(table.table).values(id=entity_id))
    closure.add_entities(conn, [entity_id])
    fts.sync_entities(conn, [entity_id], theme_text_source(schema))
    return entity_id


def _link(schema: ThemeSchema, name: str) -> RelationshipTable:
    link = schema.relationships.get(name)
    if link is None:
        raise RelationError(f"There's no {name!r} relationship.")
    return link


def _item(conn: Connection, entity_id: int) -> tuple[str, str]:
    row = conn.execute(select(Entity.type, Entity.title).where(Entity.id == entity_id)).first()
    if row is None:
        raise RelationError("That item no longer exists; press F5.")
    return row.type, row.title
