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
from tagalot.core.models import Entity, FieldProvenance, FieldSource, NoteKeyRemoval, utcnow
from tagalot.core.notes import EXTRA
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


# --- extra fields (#95) ---


@dataclass(frozen=True)
class ExtraChange:
    """Everything needed to undo and redo one change to an entity's extra fields."""

    label: str
    entity_id: int
    before: dict[str, Any]
    after: dict[str, Any]
    name: str = ""
    """The field changed."""
    source_before: FieldSource | None = None
    """Its provenance (``extra:<name>``): ``note`` for a field from the item's note (#380)."""
    source_after: FieldSource | None = None
    removed_before: bool = False
    """Whether the item's note is to leave this key off (``note_key_removal``)."""
    removed_after: bool = False


def edit_extra(
    conn: Connection,
    schema: ThemeSchema,
    entity_id: int,
    name: str,
    value: str | None,
    *,
    new: bool = False,
) -> ExtraChange:
    """Add (``new``), change, or remove (``value=None``, or empty) an extra field: a name
    and a text value the user keeps on an entity, searchable as text.

    A field from the item's note (#380) becomes the user's when changed (provenance
    ``user``, so scans leave it), and removing one is remembered (``note_key_removal``), so
    later scans leave it off."""
    row = conn.execute(select(Entity.title, Entity.extra).where(Entity.id == entity_id)).first()
    if row is None:
        raise FieldEditError("This item no longer exists")
    name = name.strip()
    if not name:
        raise FieldEditError("A field needs a name")
    before = dict(row.extra or {})
    after = dict(before)
    text = value.strip() if isinstance(value, str) else None
    if new:
        if name in before:
            raise FieldEditError(f"{row.title!r} already has a field named {name!r}")
        if not text:
            raise FieldEditError(f"Type a value for {name!r}")
        after[name] = text
        label = f"Add {name!r} to {row.title!r}"
    elif not text:
        if name not in before:
            raise FieldEditError(f"{row.title!r} has no field named {name!r}")
        del after[name]
        label = f"Remove {name!r} from {row.title!r}"
    else:
        after[name] = text
        label = f"Set {name} of {row.title!r} to {text!r}"
    source_before = conn.scalar(
        select(FieldProvenance.source).where(
            FieldProvenance.entity_id == entity_id, FieldProvenance.field == EXTRA + name
        )
    )
    removed_before = _removed(conn, entity_id, name)
    source_after, removed_after = source_before, removed_before
    if source_before is not None:  # it came from the note
        if name in after:
            source_after = FieldSource.USER
        else:
            source_after, removed_after = None, True
    _write_extra(conn, schema, entity_id, after)
    _write_note_state(conn, entity_id, name, source_after, removed_after)
    return ExtraChange(
        label,
        entity_id,
        before,
        after,
        name,
        source_before,
        source_after,
        removed_before,
        removed_after,
    )


def restore_extra(
    conn: Connection, schema: ThemeSchema, change: ExtraChange, *, forward: bool
) -> None:
    """Put an entity's extra fields back as they were before the change, or after it."""
    if conn.scalar(select(Entity.id).where(Entity.id == change.entity_id)) is None:
        return
    _write_extra(conn, schema, change.entity_id, change.after if forward else change.before)
    if change.name:
        _write_note_state(
            conn,
            change.entity_id,
            change.name,
            change.source_after if forward else change.source_before,
            change.removed_after if forward else change.removed_before,
        )


def _write_extra(
    conn: Connection, schema: ThemeSchema, entity_id: int, extra: dict[str, Any]
) -> None:
    conn.execute(
        update(Entity).where(Entity.id == entity_id).values(extra=extra, updated_at=utcnow())
    )
    fts.sync_entities(conn, [entity_id], theme_text_source(schema))


def _removed(conn: Connection, entity_id: int, name: str) -> bool:
    key = name.casefold()
    found = conn.scalar(
        select(NoteKeyRemoval.key).where(
            NoteKeyRemoval.entity_id == entity_id, NoteKeyRemoval.key == key
        )
    )
    return found is not None


def _write_note_state(
    conn: Connection, entity_id: int, name: str, source: FieldSource | None, removed: bool
) -> None:
    """Set an extra field's provenance, and whether the item's note is to leave it off."""
    field_name = EXTRA + name
    if source is None:
        conn.execute(
            delete(FieldProvenance).where(
                FieldProvenance.entity_id == entity_id, FieldProvenance.field == field_name
            )
        )
    else:
        stmt = sqlite_insert(FieldProvenance).values(
            entity_id=entity_id, field=field_name, source=source, updated_at=utcnow()
        )
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["entity_id", "field"],
                set_={"source": stmt.excluded.source, "updated_at": stmt.excluded.updated_at},
            )
        )
    key = name.casefold()
    if removed:
        conn.execute(
            sqlite_insert(NoteKeyRemoval)
            .values(entity_id=entity_id, key=key)
            .on_conflict_do_nothing()
        )
    else:
        conn.execute(
            delete(NoteKeyRemoval).where(
                NoteKeyRemoval.entity_id == entity_id, NoteKeyRemoval.key == key
            )
        )
