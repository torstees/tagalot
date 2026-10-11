"""Front-matter keys that notes never make Extra fields (DESIGN.md §9 *Notes*, #381).

Three lists apply together (:func:`ignored_keys`): the built-in defaults, the user's
(``settings.toml``, every keep), and the keep's (``keep.toml``). Keys are compared ignoring
case; ``tagalot-…`` keys are Tagalot's own and can't be ignored.

- **At scans** (:func:`sync_ignored`): the keep remembers the keys its notes were last
  applied with (``note_ignored_key``); when the lists say otherwise, every note is read
  again, which drops the Extra fields of keys now ignored and brings back those of keys no
  longer ignored.
- **From an item's page** (:func:`ignore_note_key`): ignoring a key at once removes the
  Extra fields that notes gave under it, in every item (one undo step,
  :func:`restore_note_key`); fields the user added or edited stay.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Connection, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core import fts
from tagalot.core.ingest import theme_text_source
from tagalot.core.models import (
    Entity,
    EntityNote,
    FieldProvenance,
    FieldSource,
    NoteIgnoredKey,
    utcnow,
)
from tagalot.core.notes import DEFAULT_IGNORED_KEYS, EXTRA, RESERVED_PREFIX
from tagalot.core.theme_schema import ThemeSchema


class NoteKeyError(ValueError):
    """The key can't be ignored; the message is meant for the user."""


def check_key(key: str) -> str:
    """``key`` as it is listed (stripped), or :class:`NoteKeyError`."""
    key = key.strip()
    if not key:
        raise NoteKeyError("Type a key to ignore")
    if key.casefold().startswith(RESERVED_PREFIX):
        raise NoteKeyError(f"Keys starting with {RESERVED_PREFIX!r} are Tagalot's own")
    return key


def ignored_keys(*lists: Iterable[str]) -> frozenset[str]:
    """The keys to ignore, case-folded: the built-in ones and those of ``lists``."""
    keys = {k.strip().casefold() for keys in lists for k in keys}
    return frozenset(
        DEFAULT_IGNORED_KEYS | {k for k in keys if k and not k.startswith(RESERVED_PREFIX)}
    )


def with_key(keys: Iterable[str], key: str, ignored: bool) -> list[str]:
    """A list of keys with ``key`` added (at the end, unless there) or removed, case aside."""
    folded = key.casefold()
    kept = [k for k in keys if k.casefold() != folded]
    return [*kept, key] if ignored else kept


def sync_ignored(conn: Connection, keys: frozenset[str]) -> bool:
    """Remember ``keys`` as the ones notes are applied with; when they differ from the last
    ones, mark every note file to be read again. Returns whether they differed."""
    applied = set(conn.scalars(select(NoteIgnoredKey.key)))
    if applied == keys:
        return False
    conn.execute(update(EntityNote).where(EntityNote.resource_id.is_not(None)).values(size=None))
    conn.execute(delete(NoteIgnoredKey))
    if keys:
        conn.execute(sqlite_insert(NoteIgnoredKey), [{"key": k} for k in sorted(keys)])
    return True


@dataclass(frozen=True)
class NoteKeyChange:
    """Everything needed to undo and redo ignoring a key in every note."""

    label: str
    key: str
    """As the user listed it."""
    removed: tuple[tuple[int, str, str], ...]
    """``(entity, Extra field name, value)``: the notes' fields it removed."""
    listed: bool = False
    """The keep's list had the key already, so undoing leaves it there."""


def ignore_note_key(
    conn: Connection, schema: ThemeSchema, key: str, *, listed: bool = False
) -> NoteKeyChange:
    """Ignore ``key`` in every note: remove the Extra fields notes gave under it (a DB
    writer job). The caller adds it to ``keep.toml``."""
    key = check_key(key)
    folded = key.casefold()
    found = conn.execute(
        select(FieldProvenance.entity_id, FieldProvenance.field, Entity.extra)
        .join(Entity, Entity.id == FieldProvenance.entity_id)
        .where(
            FieldProvenance.field.startswith(EXTRA, autoescape=True),
            FieldProvenance.source == FieldSource.NOTE,
        )
    ).all()
    removed = tuple(
        (entity_id, name, str(extra[name]))
        for entity_id, field_name, extra in found
        if (name := field_name[len(EXTRA) :]).casefold() == folded and name in (extra or {})
    )
    items = len({entity_id for entity_id, _, _ in removed})
    label = f"Ignore {key!r} in every note" + (
        f" ({items} item{'s' if items != 1 else ''})" if items else ""
    )
    change = NoteKeyChange(label, key, removed, listed)
    restore_note_key(conn, schema, change, forward=True)
    return change


def restore_note_key(
    conn: Connection, schema: ThemeSchema, change: NoteKeyChange, *, forward: bool
) -> None:
    """Remove the fields again (``forward``) or put them back, with their provenance, and
    remember the key as applied or not. Items deleted since are left out."""
    by_entity: dict[int, dict[str, str]] = {}
    for entity_id, name, value in change.removed:
        by_entity.setdefault(entity_id, {})[name] = value
    rows = conn.execute(select(Entity.id, Entity.extra).where(Entity.id.in_(by_entity))).all()
    for entity_id, extra in rows:
        fields = by_entity[entity_id]
        after = dict(extra or {})
        if forward:
            for name in fields:
                after.pop(name, None)
            conn.execute(
                delete(FieldProvenance).where(
                    FieldProvenance.entity_id == entity_id,
                    FieldProvenance.field.in_([EXTRA + n for n in fields]),
                )
            )
        else:
            after.update(fields)
            stmt = sqlite_insert(FieldProvenance)
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["entity_id", "field"],
                    set_={"source": stmt.excluded.source, "updated_at": stmt.excluded.updated_at},
                ),
                [
                    {
                        "entity_id": entity_id,
                        "field": EXTRA + n,
                        "source": FieldSource.NOTE,
                        "updated_at": utcnow(),
                    }
                    for n in fields
                ],
            )
        conn.execute(
            update(Entity).where(Entity.id == entity_id).values(extra=after, updated_at=utcnow())
        )
    fts.sync_entities(conn, [r.id for r in rows], theme_text_source(schema))
    folded = change.key.casefold()
    if forward:
        conn.execute(sqlite_insert(NoteIgnoredKey).values(key=folded).on_conflict_do_nothing())
    elif folded not in DEFAULT_IGNORED_KEYS:
        conn.execute(delete(NoteIgnoredKey).where(NoteIgnoredKey.key == folded))
