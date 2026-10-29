"""Moving values between Extra fields and the keep's own fields (#582, DESIGN.md §12 *Fields
of your own*).

**Make it a field…** promotes an Extra field: its values that read as the new field's kind
(:func:`parse_as`) move into it, keeping their provenance (a note's stays the note's, a
user's edit stays the user's); those that don't stay Extra fields. **Turn back into an Extra
field** (and Keep configuration's Remove) does the reverse: the field's values become text
Extra fields named by its label, and the field leaves ``keep.toml``.

Each is one undo step, a :class:`FieldMove`: whole-item snapshots before and after (they
carry Extra fields, own fields' values, and provenance), and ``keep.toml``'s fields before
and after, which the session writes back as the step is undone and redone.
"""

import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Connection, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core import fts
from tagalot.core.actions import ActionChange
from tagalot.core.entity_state import ChangeRecorder
from tagalot.core.extra_keys import EXTRA, as_text
from tagalot.core.ingest import theme_text_source
from tagalot.core.models import Entity, EntityField, FieldProvenance, FieldSource, utcnow
from tagalot.core.own_fields import OwnField, OwnFields, rating_problem
from tagalot.core.own_values import VALUE_ROW, read_value, store_value
from tagalot.core.theme_schema import ThemeSchema

_INTEGER = re.compile(r"[+-]?(\d{1,3}(,\d{3})+|\d+)\Z")
_YES = frozenset({"yes", "y", "true", "1"})
_NO = frozenset({"no", "n", "false", "0"})


def parse_as(kind: str, text: str) -> Any:
    """``text`` (an Extra field's value) as a value of ``kind``; ``ValueError`` when it
    doesn't read as one. Whole numbers may group thousands with commas (``1,995``); dates
    are ``YYYY-MM-DD``; yes/no is yes, no, true, false, y, n, 1, or 0; a link is a web
    address (``http`` or ``https``)."""
    text = text.strip()
    if not text:
        raise ValueError("empty")
    match kind:
        case "text":
            return text
        case "link":
            if not re.match(r"https?://\S+\Z", text, re.IGNORECASE):
                raise ValueError("not a web address")
            return text
        case "integer":
            if not _INTEGER.match(text):
                raise ValueError("not a whole number")
            return int(text.replace(",", ""))
        case "number" | "rating":
            number = float(text.replace(",", ""))
            if not math.isfinite(number):
                raise ValueError("not a number")
            if kind == "rating" and (problem := rating_problem(number)) is not None:
                raise ValueError(problem)
            return number
        case "date":
            if len(text) != 10:
                raise ValueError("not a date")
            return date.fromisoformat(text)
        case "yes/no":
            folded = text.casefold()
            if folded in _YES:
                return True
            if folded in _NO:
                return False
            raise ValueError("not yes or no")
    raise ValueError(f"no kind {kind!r}")


def fits(kind: str, text: str) -> bool:
    try:
        parse_as(kind, text)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class KeyValue:
    """An item's Extra field under a key (matched case aside)."""

    entity_id: int
    type_id: str
    title: str
    key: str
    """The key as the item has it."""
    text: str


def key_values(conn: Connection, key: str) -> list[KeyValue]:
    """Every item's Extra field named ``key`` (case aside), by title. Reads every item
    with Extra fields: call it from a worker."""
    folded = key.strip().casefold()
    found = []
    rows = conn.execute(
        select(Entity.id, Entity.type, Entity.title, Entity.extra).where(Entity.extra.is_not(None))
    )
    for entity_id, type_id, title, extra in rows:
        for name, value in (extra or {}).items():
            if name.casefold() == folded:
                text = as_text(value)
                if text is not None:
                    found.append(KeyValue(entity_id, type_id, title, name, text))
                break
    found.sort(key=lambda v: v.title.casefold())
    return found


def type_counts(values: Iterable[KeyValue]) -> dict[str, int]:
    """How many items of each type have the key."""
    counts: dict[str, int] = {}
    for value in values:
        counts[value.type_id] = counts.get(value.type_id, 0) + 1
    return counts


@dataclass(frozen=True)
class Preview:
    """What promoting would do, for the dialog."""

    items: int
    """Items of the chosen types with the key."""
    fit: int
    """Those whose value reads as the kind: they move."""
    misfits: tuple[KeyValue, ...]
    """Those whose value doesn't: they stay Extra fields."""


def preview(values: Iterable[KeyValue], kind: str, type_ids: Iterable[str]) -> Preview:
    chosen = set(type_ids)
    mine = [v for v in values if v.type_id in chosen]
    misfits = tuple(v for v in mine if not fits(kind, v.text))
    return Preview(len(mine), len(mine) - len(misfits), misfits)


@dataclass(frozen=True)
class FieldMove:
    """One promotion, or one field turned back into Extra fields: an undo step."""

    label: str
    action: ActionChange
    fields_before: OwnFields
    fields_after: OwnFields
    moved: int
    """How many items' values moved."""


def promote(conn: Connection, schema: ThemeSchema, key: str, fields: Sequence[OwnField]) -> int:
    """Move the Extra fields named ``key`` (case aside) of items of these fields' types,
    whose values read as the field's kind, into the field of their type. Returns how many
    moved. The caller snapshots the items and saves ``keep.toml``."""
    by_type = {f.type_id: f for f in fields}
    moved = []
    for value in key_values(conn, key):
        own = by_type.get(value.type_id)
        if own is None:
            continue
        try:
            parsed = parse_as(own.kind, value.text)
        except ValueError:
            continue
        source = _source(conn, value.entity_id, EXTRA + value.key)
        extra = dict(conn.scalar(select(Entity.extra).where(Entity.id == value.entity_id)) or {})
        extra.pop(value.key, None)
        conn.execute(
            update(Entity)
            .where(Entity.id == value.entity_id)
            .values(extra=extra, updated_at=utcnow())
        )
        store_value(conn, value.entity_id, own.name, parsed)
        _set_source(conn, value.entity_id, EXTRA + value.key, None)
        # Typed by the user as an Extra field (no provenance): the user's.
        _set_source(conn, value.entity_id, own.key, source or FieldSource.USER)
        moved.append(value.entity_id)
    fts.sync_entities(conn, moved, theme_text_source(schema))
    return len(moved)


def turn_back(conn: Connection, schema: ThemeSchema, own: OwnField) -> int:
    """Put an own field's values back as text Extra fields named by its label (``"<label>
    (<name>)"`` where that name holds something else), keeping a note's or a file's
    provenance; the user's values become the user's own Extra fields. Returns how many
    items changed. The caller snapshots the items and saves ``keep.toml``."""
    rows = conn.execute(
        select(*VALUE_ROW, Entity.extra)
        .join(Entity, Entity.id == EntityField.entity_id)
        .where(EntityField.field == own.name, Entity.type == own.type_id)
    ).all()
    ids = []
    for row in rows:
        text = as_text(read_value(own, row))
        source = _source(conn, row.entity_id, own.key)
        store_value(conn, row.entity_id, own.name, None)
        _set_source(conn, row.entity_id, own.key, None)
        if text is None:
            continue
        extra = dict(row.extra or {})
        key = own.label
        if key in extra and extra[key] != text:
            key = f"{own.label} ({own.name})"
        extra[key] = text
        conn.execute(
            update(Entity)
            .where(Entity.id == row.entity_id)
            .values(extra=extra, updated_at=utcnow())
        )
        given = source in (FieldSource.NOTE, FieldSource.EXTRACTED)
        _set_source(conn, row.entity_id, EXTRA + key, source if given else None)
        ids.append(row.entity_id)
    fts.sync_entities(conn, ids, theme_text_source(schema))
    return len(ids)


def recorded(
    conn: Connection,
    schema: ThemeSchema,
    ids: Iterable[int],
    move: Callable[[Connection], int],
) -> tuple[ActionChange, int]:
    """Run ``move`` with snapshots of the items it may change, before and after."""
    recorder = ChangeRecorder(conn, schema)
    recorder.touch(ids)
    moved = move(conn)
    return ActionChange("", recorder.before, recorder.after()), moved


def promotion_items(conn: Connection, key: str, type_ids: Iterable[str]) -> list[int]:
    chosen = set(type_ids)
    return [v.entity_id for v in key_values(conn, key) if v.type_id in chosen]


def field_items(conn: Connection, own: OwnField) -> list[int]:
    return list(
        conn.scalars(
            select(EntityField.entity_id)
            .join(Entity, Entity.id == EntityField.entity_id)
            .where(EntityField.field == own.name, Entity.type == own.type_id)
        )
    )


def _source(conn: Connection, entity_id: int, name: str) -> FieldSource | None:
    return conn.scalar(
        select(FieldProvenance.source).where(
            FieldProvenance.entity_id == entity_id, FieldProvenance.field == name
        )
    )


def _set_source(conn: Connection, entity_id: int, name: str, source: FieldSource | None) -> None:
    if source is None:
        conn.execute(
            delete(FieldProvenance).where(
                FieldProvenance.entity_id == entity_id, FieldProvenance.field == name
            )
        )
        return
    stmt = sqlite_insert(FieldProvenance).values(
        entity_id=entity_id, field=name, source=source, updated_at=utcnow()
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["entity_id", "field"],
            set_={"source": stmt.excluded.source, "updated_at": stmt.excluded.updated_at},
        )
    )
