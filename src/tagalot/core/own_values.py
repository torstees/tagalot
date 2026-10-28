"""The values of a keep's own fields (#581, ``core/own_fields.py``): the ``entity_field``
table.

Each value is a row in the column of its kind, so it sorts and filters by its index
(#583). Edits go through :func:`tagalot.core.fields.edit_own_field`, one undo step with
provenance ``user`` (``own:<name>``), as a theme field's do. Removing a field puts its
values back as Extra fields (:func:`remove_field_values`).
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from sqlalchemy import Connection, delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core import fts
from tagalot.core.extra_keys import as_text
from tagalot.core.models import Entity, EntityField, FieldProvenance, utcnow
from tagalot.core.own_fields import OwnField

_COLUMNS = ("text", "integer", "real", "day")
_ROW = (
    EntityField.entity_id,
    EntityField.field,
    EntityField.text,
    EntityField.integer,
    EntityField.real,
    EntityField.day,
)
"""A stored value's row, as :func:`read_value` reads it."""


def _column(value: Any) -> str:
    """The column a value is stored in."""
    if isinstance(value, bool | int):
        return "integer"
    if isinstance(value, float):
        return "real"
    if isinstance(value, date):
        return "day"
    return "text"


def store_value(conn: Connection, entity_id: int, name: str, value: Any) -> None:
    """Set an item's value of the own field ``name``; None removes it. The caller keeps
    provenance and the search index in step (``core.fields`` does)."""
    if value is None or value == "":
        conn.execute(
            delete(EntityField).where(EntityField.entity_id == entity_id, EntityField.field == name)
        )
        return
    values: dict[str, Any] = dict.fromkeys(_COLUMNS)
    values[_column(value)] = int(value) if isinstance(value, bool) else value
    stmt = sqlite_insert(EntityField).values(entity_id=entity_id, field=name, **values)
    conn.execute(stmt.on_conflict_do_update(index_elements=["entity_id", "field"], set_=values))


def read_value(own: OwnField, row: Any) -> Any:
    """A stored row's value as the field has it (yes/no as a bool)."""
    if own.kind == "yes/no":
        return None if row.integer is None else bool(row.integer)
    if own.kind == "integer":
        return row.integer
    if own.kind == "number":
        return row.real
    if own.kind == "date":
        return row.day
    return row.text


def item_values(conn: Connection, entity_id: int, fields: Iterable[OwnField]) -> dict[str, Any]:
    """An item's values of these own fields, by name (fields without a value left out)."""
    by_name = {f.name: f for f in fields}
    if not by_name:
        return {}
    rows = conn.execute(
        select(*_ROW).where(
            EntityField.entity_id == entity_id, EntityField.field.in_(list(by_name))
        )
    )
    return {r.field: read_value(by_name[r.field], r) for r in rows}


def raw_values(conn: Connection, entity_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Every own-field value of these items as stored, by item and field name (for
    snapshots and merges, which don't need the fields' kinds)."""
    found: dict[int, dict[str, Any]] = {}
    for row in conn.execute(select(*_ROW).where(EntityField.entity_id.in_(entity_ids))):
        value = next((v for c in _COLUMNS if (v := getattr(row, c)) is not None), None)
        found.setdefault(row.entity_id, {})[row.field] = value
    return found


def texts(conn: Connection, entity_ids: Sequence[int]) -> dict[int, list[str]]:
    """The text values (text and link fields) of these items, for the search index."""
    found: dict[int, list[str]] = {}
    rows = conn.execute(
        select(EntityField.entity_id, EntityField.text).where(
            EntityField.entity_id.in_(entity_ids), EntityField.text.is_not(None)
        )
    )
    for entity_id, text in rows:
        if text is not None:
            found.setdefault(entity_id, []).append(text)
    return found


def count_values(conn: Connection, own: OwnField) -> int:
    """How many items of the field's type have a value of it."""
    return int(
        conn.scalar(
            select(func.count())
            .select_from(EntityField)
            .join(Entity, Entity.id == EntityField.entity_id)
            .where(EntityField.field == own.name, Entity.type == own.type_id)
        )
        or 0
    )


def remove_field_values(
    conn: Connection, own: OwnField, text_source: fts.TextSource = fts.no_text_fields
) -> int:
    """Put the field's values back as Extra fields named by its label, and delete them
    (Keep configuration's Remove, #581). An item whose Extra field of that name already
    holds something else gets ``"<label> (<name>)"``. Returns how many items changed."""
    rows = conn.execute(
        select(*_ROW, Entity.extra)
        .join(Entity, Entity.id == EntityField.entity_id)
        .where(EntityField.field == own.name, Entity.type == own.type_id)
    ).all()
    ids = []
    for row in rows:
        text = as_text(read_value(own, row))
        if text is None:
            continue
        after = dict(row.extra or {})
        key = own.label
        if key in after and after[key] != text:
            key = f"{own.label} ({own.name})"
        after[key] = text
        conn.execute(
            update(Entity)
            .where(Entity.id == row.entity_id)
            .values(extra=after, updated_at=utcnow())
        )
        ids.append(row.entity_id)
    of_type = select(Entity.id).where(Entity.type == own.type_id)
    conn.execute(
        delete(EntityField).where(EntityField.field == own.name, EntityField.entity_id.in_(of_type))
    )
    conn.execute(
        delete(FieldProvenance).where(
            FieldProvenance.field == own.key, FieldProvenance.entity_id.in_(of_type)
        )
    )
    fts.sync_entities(conn, ids, text_source)
    return len(ids)


def restore_raw(conn: Connection, values: Mapping[int, Mapping[str, Any]]) -> None:
    """Make these items' own-field values exactly ``values`` (undoing a merge or an
    action's change)."""
    ids = list(values)
    conn.execute(delete(EntityField).where(EntityField.entity_id.in_(ids)))
    existing = set(conn.scalars(select(Entity.id).where(Entity.id.in_(ids))))
    for entity_id, by_name in values.items():
        if entity_id in existing:
            for name, value in by_name.items():
                store_value(conn, entity_id, name, value)
