"""Relating items by hand: adding someone to a cast, or removing them (DESIGN.md §9, §12;
#260).

Each is one undo step, an :class:`~tagalot.core.actions.ActionChange` over both items'
snapshots (which carry the relationship and the user's record of it). The record, in
``user_relation``, is what scans respect: a theme's ``unrelate`` leaves a relationship the
user added, and its ``relate`` doesn't bring back one the user removed.
"""

from sqlalchemy import Connection, delete, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core import closure, fts
from tagalot.core.actions import ActionChange
from tagalot.core.entity_state import ChangeRecorder
from tagalot.core.ingest import next_entity_id, theme_text_source
from tagalot.core.models import Entity, UserRelation, utcnow
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
) -> ActionChange:
    """Relate ``entity_id`` and ``other_id`` through relationship ``name`` (either may be
    the ``a`` side), or, with ``new_title``, a new item of the other side's type made for
    it (an item the user made: no ingest key, so scans never delete it)."""
    link = _link(schema, name)
    rel = link.relationship
    entity_type, title = _item(conn, entity_id)
    a_type, b_type = (schema.theme.type_id_of(rel.a), schema.theme.type_id_of(rel.b))
    if entity_type == a_type:
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
        if found_type != other_type:
            raise RelationError(f"{other_title} can't be added there.")
        recorder.touch([other_id])
    a_id, b_id = (entity_id, other_id) if this_is_a else (other_id, entity_id)
    t = link.table
    if not rel.many:  # b's one partner is replaced: the old one is touched too
        recorder.touch(conn.scalars(select(t.c.a_id).where(t.c.b_id == b_id)))
        conn.execute(delete(t).where(t.c.b_id == b_id, t.c.a_id != a_id))
    conn.execute(insert(t).values(a_id=a_id, b_id=b_id).prefix_with("OR IGNORE"))
    _record(conn, name, a_id, b_id, added=True)
    label = (rel.label if this_is_a else rel.reverse_label) or name
    return ActionChange(
        f"Add {other_title!r} to {title}'s {label.lower()}",
        recorder.before,
        recorder.after(),
    )


def remove_related(
    conn: Connection, schema: ThemeSchema, name: str, entity_id: int, other_id: int
) -> ActionChange:
    """Unrelate the two items (in either order), remembering it, so a theme reading its
    files again doesn't relate them anew."""
    link = _link(schema, name)
    _, title = _item(conn, entity_id)
    _, other_title = _item(conn, other_id)
    t = link.table
    pairs = [(entity_id, other_id), (other_id, entity_id)]
    found = next(
        (
            (a, b)
            for a, b in pairs
            if conn.scalar(select(t.c.a_id).where(t.c.a_id == a, t.c.b_id == b)) is not None
        ),
        None,
    )
    if found is None:
        raise RelationError(f"{other_title} isn't related to {title} there.")
    recorder = ChangeRecorder(conn, schema)
    recorder.touch([entity_id, other_id])
    a_id, b_id = found
    conn.execute(delete(t).where(t.c.a_id == a_id, t.c.b_id == b_id))
    _record(conn, name, a_id, b_id, added=False)
    rel = link.relationship
    this_is_a = a_id == entity_id
    label = (rel.label if this_is_a else rel.reverse_label) or name
    return ActionChange(
        f"Remove {other_title!r} from {title}'s {label.lower()}",
        recorder.before,
        recorder.after(),
    )


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
