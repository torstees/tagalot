"""Editing an entity's fields by hand, with provenance and undo (DESIGN.md §5, §12).

:func:`edit_field` sets one field (or the title) in a DB-writer transaction, marks it
``user`` so extraction never overwrites it, refreshes the entity's search row, and returns
a :class:`FieldChange` that :func:`restore_field` can undo and redo.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Connection, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core import fts
from tagalot.core.ingest import TITLE, theme_text_source
from tagalot.core.models import Entity, FieldProvenance, FieldSource, utcnow
from tagalot.core.theme_schema import EntityTable, ThemeSchema
from tagalot.themes.api import FieldInfo, entity_label


class FieldEditError(ValueError):
    """The edit can't be made; the message is meant for the user."""


@dataclass(frozen=True)
class FieldChange:
    """Everything needed to undo and redo one field edit."""

    label: str
    """What the user did, e.g. ``Set Year of 'Heat' to 1995``."""
    entity_id: int
    field: str
    """A theme field's name, or ``"title"``."""
    before: Any
    after: Any
    source_before: FieldSource | None
    source_after: FieldSource | None


def check_value(info: FieldInfo, value: Any) -> Any:
    """``value`` as the field stores it, or :class:`FieldEditError` if it doesn't fit."""
    if value is None or value == "":
        if info.nullable:
            return None
        raise FieldEditError(f"{info.spec.label} can't be empty")
    kind = info.type
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if kind is date and isinstance(value, datetime):
        raise FieldEditError(f"{info.spec.label} is a date, not a date and time")
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise FieldEditError(f"{info.spec.label} must be {_kind(kind)}")
    if isinstance(value, datetime):
        return value.astimezone(UTC)  # a time without a zone is the user's local time
    return value.strip() if isinstance(value, str) else value


def edit_field(
    conn: Connection, schema: ThemeSchema, entity_id: int, name: str, value: Any
) -> FieldChange:
    """Set a field (or the title, ``name="title"``) to a value the user chose."""
    row = conn.execute(select(Entity.type, Entity.title).where(Entity.id == entity_id)).first()
    if row is None:
        raise FieldEditError("This item no longer exists")
    table = schema.by_type_id(row.type)
    if name == TITLE:
        title = value.strip() if isinstance(value, str) else ""
        if not title:
            raise FieldEditError(f"{table.entity.title_label} can't be empty")
        before, after, label = row.title, title, table.entity.title_label
    else:
        info = _field(table, name)
        if not info.spec.editable:
            raise FieldEditError(f"{info.spec.label} can't be edited")
        after = check_value(info, value)
        before = conn.scalar(select(table.table.c[name]).where(table.table.c.id == entity_id))
        label = info.spec.label
    source_before = conn.scalar(
        select(FieldProvenance.source).where(
            FieldProvenance.entity_id == entity_id, FieldProvenance.field == name
        )
    )
    _write(conn, schema, table, entity_id, name, after, FieldSource.USER)
    shown = "(empty)" if after is None else repr(after) if isinstance(after, str) else after
    return FieldChange(
        f"Set {label} of {row.title!r} to {shown}",
        entity_id,
        name,
        before,
        after,
        source_before,
        FieldSource.USER,
    )


def restore_field(
    conn: Connection, schema: ThemeSchema, change: FieldChange, *, forward: bool
) -> None:
    """Put a field back as it was before the edit (undo), or as the edit left it (redo)."""
    row = conn.execute(select(Entity.type).where(Entity.id == change.entity_id)).first()
    if row is None:
        return  # deleted since: nothing to put back
    value = change.after if forward else change.before
    source = change.source_after if forward else change.source_before
    _write(conn, schema, schema.by_type_id(row.type), change.entity_id, change.field, value, source)


def _write(
    conn: Connection,
    schema: ThemeSchema,
    table: EntityTable,
    entity_id: int,
    name: str,
    value: Any,
    source: FieldSource | None,
) -> None:
    if name == TITLE:
        conn.execute(update(Entity).where(Entity.id == entity_id).values(title=value))
    else:
        conn.execute(update(table.table).where(table.table.c.id == entity_id).values({name: value}))
    conn.execute(update(Entity).where(Entity.id == entity_id).values(updated_at=utcnow()))
    if source is None:
        conn.execute(
            delete(FieldProvenance).where(
                FieldProvenance.entity_id == entity_id, FieldProvenance.field == name
            )
        )
    else:
        stmt = sqlite_insert(FieldProvenance).values(
            entity_id=entity_id, field=name, source=source, updated_at=utcnow()
        )
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["entity_id", "field"],
                set_={"source": stmt.excluded.source, "updated_at": stmt.excluded.updated_at},
            )
        )
    fts.sync_entities(conn, [entity_id], theme_text_source(schema))


def _field(table: EntityTable, name: str) -> FieldInfo:
    for info in table.fields:
        if info.name == name:
            return info
    raise FieldEditError(f"{entity_label(table.entity)} has no field {name!r}")


def _kind(kind: type) -> str:
    names = {
        str: "text",
        int: "a whole number",
        float: "a number",
        bool: "yes or no",
        date: "a date (YYYY-MM-DD)",
        datetime: "a date and time",
    }
    return names.get(kind, kind.__name__)


def user_fields(conn: Connection, entity_id: int) -> set[str]:
    """The fields (and ``"title"``) the user has edited on an entity."""
    return set(
        conn.scalars(
            select(FieldProvenance.field).where(
                FieldProvenance.entity_id == entity_id,
                FieldProvenance.source == FieldSource.USER,
            )
        )
    )
