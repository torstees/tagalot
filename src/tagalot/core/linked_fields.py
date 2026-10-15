"""Editing fields that mirror links (DESIGN.md §9 *Fields*, #440).

A theme's text field can be a copy of a link (``field(..., mirrors=...)``, theme API 15):
the titles of the item's containers of a type (``mirrors=Series``: a book's **Series**,
**Collections**), or
of the items on the other side of a relationship (its **Author**, ``"writers"``). The theme
still writes both from the files. Editing such a field (:func:`edit_linked_field`) changes
the links:

- each name is found or made with the theme's :meth:`~tagalot.themes.api.Theme.item_named`
  (so an edit and a later scan agree on one item);
- the item is put in, or related to, those items, and taken out of, or unrelated from, the
  others of that type, **by hand** (``user_contains``, ``user_relation``), so scans keep it;
- the field is the user's (provenance ``user``), as the found items' titles say it.

It is one undo step, through the same whole-item snapshots as theme actions. Several names
are separated by ``;`` when the text has one, else by ``,`` (``Pratchett, Terry`` needs
``;`` between authors).

A containment field the item gets **through another container** (a book's universe, when
its series is in one: ``contains(Universe, Series)``) can't be edited on the item
(:func:`read_only_reason`): it follows the container, and is brought up to date, as the
user's, whenever an edit moves the item or its container (:func:`refresh_indirect`).
"""

from collections.abc import Iterable, Sequence
from typing import Any, TypeGuard

from sqlalchemy import Connection, delete, insert, select

from tagalot.core import closure
from tagalot.core.actions import ActionChange
from tagalot.core.containers import record_contains
from tagalot.core.entity_state import ChangeRecorder
from tagalot.core.fields import FieldEditError, write_field
from tagalot.core.ingest import IngestSession
from tagalot.core.models import Entity, EntityContains, FieldSource
from tagalot.core.relations import record_relation
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import FieldInfo, Theme, entity_label


def _is_type(mirror: object) -> TypeGuard[type[ThemeEntity]]:
    """A mirror that is a container type (else a relationship's name)."""
    return isinstance(mirror, type)


def names_in(text: str | None) -> list[str]:
    """The names a mirrored field's text gives, each once (case aside), in order."""
    if not text or not text.strip():
        return []
    parts = text.split(";") if ";" in text else text.split(",")
    found: dict[str, str] = {}
    for part in parts:
        name = " ".join(part.split())
        if name and name.casefold() not in found:
            found[name.casefold()] = name
    return list(found.values())


def _type_id(schema: ThemeSchema, kind: type) -> str:
    return schema.theme.type_id_of(kind)


def _parents(conn: Connection, entity_id: int) -> list[tuple[int, str, str]]:
    """The item's direct containers: id, type, title."""
    return [
        (int(i), t, title)
        for i, t, title in conn.execute(
            select(Entity.id, Entity.type, Entity.title)
            .join(EntityContains, EntityContains.parent_id == Entity.id)
            .where(EntityContains.child_id == entity_id)
            .order_by(Entity.title)
        )
    ]


Parent = tuple[int, str, str]
"""A container: id, type, title."""


def _through(schema: ThemeSchema, parent: type, parents: Sequence[Parent]) -> list[Parent]:
    """The item's containers through which it gets containers of type ``parent``."""
    via = {
        _type_id(schema, c.child)
        for c in schema.theme.containment
        if c.parent is parent and c.child is not None
    }
    return [p for p in parents if p[1] in via]


def read_only_reason(
    conn: Connection, schema: ThemeSchema, entity_id: int, info: FieldInfo
) -> str | None:
    """Why a mirrored containment field can't be edited on this item: it comes through
    another container (a book's universe through its series). ``None`` if it can."""
    mirror = info.spec.mirrors
    if not _is_type(mirror):
        return None
    through = _through(schema, mirror, _parents(conn, entity_id))
    if not through:
        return None
    kind = entity_label(schema.by_type_id(through[0][1]).entity).lower()
    return f"It comes from its {kind}, {through[0][2]}: change it there."


def edit_linked_field(
    conn: Connection,
    schema: ThemeSchema,
    theme: Theme,
    entity_id: int,
    info: FieldInfo,
    value: Any,
) -> ActionChange:
    """Set a mirrored field to the names in ``value``, changing the item's links by hand.
    Raises :class:`~tagalot.core.fields.FieldEditError` for an edit that can't be made."""
    row = conn.execute(select(Entity.type, Entity.title).where(Entity.id == entity_id)).first()
    if row is None:
        raise FieldEditError("This item no longer exists")
    reason = read_only_reason(conn, schema, entity_id, info)
    if reason is not None:
        raise FieldEditError(f"{info.spec.label} can't be edited here. {reason}")
    mirror = info.spec.mirrors
    names = names_in(value if isinstance(value, str) else None)
    recorder = ChangeRecorder(conn, schema)
    recorder.touch([entity_id])
    ctx = IngestSession(conn, schema)
    ctx.recorder = recorder
    if _is_type(mirror):
        if len(names) > 1 and not info.spec.many:
            raise FieldEditError(f"{info.spec.label} takes one name")
        targets = [theme.item_named(mirror, n, ctx) for n in names]
        ctx.flush()
        _contain(conn, schema, recorder, entity_id, mirror, [t.id for t in targets])
    elif isinstance(mirror, str):
        link = schema.relationships[mirror]
        rel = link.relationship
        item_is_a = row.type == _type_id(schema, rel.a)
        other = rel.b if item_is_a else rel.a
        if len(names) > 1 and not rel.many:
            raise FieldEditError(f"{info.spec.label} takes one name")
        targets = [theme.item_named(other, n, ctx) for n in names]
        ctx.flush()
        _relate(conn, recorder, mirror, link.table, entity_id, item_is_a, [t.id for t in targets])
    else:  # pragma: no cover - the loader refuses these
        raise FieldEditError(f"{info.spec.label} can't be edited")
    titles = _titles(conn, [t.id for t in targets])
    text = ", ".join(titles) or None
    write_field(conn, schema, schema.by_type_id(row.type), entity_id, info.name, text)
    children = conn.scalars(
        select(EntityContains.child_id).where(EntityContains.parent_id == entity_id)
    ).all()
    refresh_indirect(conn, schema, recorder, [entity_id, *children])
    shown = repr(text) if text else "(empty)"
    label = f"Set {info.spec.label} of {row.title!r} to {shown}"
    return ActionChange(label, recorder.before, recorder.after())


def _titles(conn: Connection, ids: Sequence[int]) -> list[str]:
    found = dict(conn.execute(select(Entity.id, Entity.title).where(Entity.id.in_(ids))).all())
    return [found[i] for i in dict.fromkeys(ids) if i in found]


def _contain(
    conn: Connection,
    schema: ThemeSchema,
    recorder: ChangeRecorder,
    entity_id: int,
    mirror: type,
    wanted: Sequence[int],
) -> None:
    parent_type = _type_id(schema, mirror)
    current = [p for p, t, _ in _parents(conn, entity_id) if t == parent_type]
    removed = [p for p in current if p not in wanted]
    added = [p for p in dict.fromkeys(wanted) if p not in current]
    recorder.touch([*removed, *added])
    result = closure.apply(
        conn,
        added=[(p, entity_id) for p in added],
        removed=[(p, entity_id) for p in removed],
    )
    if result.rejected:
        _, why = result.rejected[0]
        raise FieldEditError(f"It can't go there: {why}")
    for parent in removed:
        record_contains(conn, parent, entity_id, added=False)
    for parent in added:
        record_contains(conn, parent, entity_id, added=True)


def _relate(
    conn: Connection,
    recorder: ChangeRecorder,
    name: str,
    table: Any,
    entity_id: int,
    item_is_a: bool,
    wanted: Sequence[int],
) -> None:
    mine, theirs = (table.c.a_id, table.c.b_id) if item_is_a else (table.c.b_id, table.c.a_id)
    current: list[int] = list(conn.scalars(select(theirs).where(mine == entity_id)))
    removed = [o for o in current if o not in wanted]
    added = [o for o in dict.fromkeys(wanted) if o not in current]
    recorder.touch([*removed, *added])

    def pair(other: int) -> tuple[int, int]:
        return (entity_id, other) if item_is_a else (other, entity_id)

    for other in removed:
        a_id, b_id = pair(other)
        conn.execute(delete(table).where(table.c.a_id == a_id, table.c.b_id == b_id))
        record_relation(conn, name, a_id, b_id, added=False)
    for other in added:
        a_id, b_id = pair(other)
        conn.execute(insert(table).values(a_id=a_id, b_id=b_id).prefix_with("OR IGNORE"))
        record_relation(conn, name, a_id, b_id, added=True)


def refresh_indirect(
    conn: Connection, schema: ThemeSchema, recorder: ChangeRecorder, ids: Iterable[int]
) -> None:
    """Bring up to date, as the user's, the mirrored containment fields these items get
    through another container (a book's universe, from its series)."""
    for entity_id in dict.fromkeys(ids):
        type_id = conn.scalar(select(Entity.type).where(Entity.id == entity_id))
        if type_id is None:
            continue
        table = schema.by_type_id(type_id)
        for info in table.fields:
            mirror = info.spec.mirrors
            if not _is_type(mirror):
                continue
            through = _through(schema, mirror, _parents(conn, entity_id))
            if not through:
                continue
            parent_type = _type_id(schema, mirror)
            titles = sorted(
                {
                    title
                    for via, _, _ in through
                    for _, t, title in _parents(conn, via)
                    if t == parent_type
                },
                key=str.casefold,
            )
            text = ", ".join(titles) or None
            current = conn.scalar(
                select(table.table.c[info.name]).where(table.table.c.id == entity_id)
            )
            if current != text:
                recorder.touch([entity_id])
                write_field(conn, schema, table, entity_id, info.name, text, FieldSource.USER)
