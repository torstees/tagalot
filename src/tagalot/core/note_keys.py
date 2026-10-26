"""Front-matter keys that notes, and Markdown files' own front matter (#410), never make
Extra fields (DESIGN.md §9 *Notes*, #381).

Three lists apply together (:func:`ignored_keys`): the built-in defaults, the user's
(``settings.toml``, every keep), and the keep's (``keep.toml``). Keys are compared ignoring
case; ``tagalot-…`` keys are Tagalot's own and can't be ignored.

- **At scans** (:func:`sync_ignored`): the keep remembers the keys its notes were last
  applied with (``note_ignored_key``); when the lists say otherwise, every note is read
  again, which drops the Extra fields of keys now ignored and brings back those of keys no
  longer ignored.
- **From an item's page** (:func:`ignore_note_key`): ignoring a key at once removes the
  Extra fields that notes and files gave under it, in every item (one undo step,
  :func:`restore_note_key`); fields the user added or edited stay.
- **Your own key aliases** (#411, :func:`key_aliases`): keys read as a field rather than
  as Extra fields, the user's and the keep's (the keep's winning); remembered as applied
  (``note_key_alias``, :func:`sync_aliases`) so a change reads notes and files again.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import Connection, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core import fts
from tagalot.core.extra_keys import (
    DEFAULT_IGNORED_KEYS,
    EXTRA,
    NOT_ALIASED,
    RESERVED_PREFIX,
    applied_aliases,
)
from tagalot.core.ingest import theme_text_source
from tagalot.core.models import (
    Entity,
    EntityNote,
    FieldProvenance,
    FieldSource,
    NoteIgnoredKey,
    NoteKeyAlias,
    Resource,
    utcnow,
)
from tagalot.core.notes import NOTE_EXTENSIONS
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
    ones, mark every note file, and every Markdown file a theme reads (its front matter's
    Extra fields, #410), to be read again. Returns whether they differed."""
    applied = set(conn.scalars(select(NoteIgnoredKey.key)))
    if applied == keys:
        return False
    _read_again(conn)
    conn.execute(delete(NoteIgnoredKey))
    if keys:
        conn.execute(sqlite_insert(NoteIgnoredKey), [{"key": k} for k in sorted(keys)])
    return True


def _read_again(conn: Connection) -> None:
    """Mark every note file and every Markdown file a theme reads to be read again."""
    conn.execute(update(EntityNote).where(EntityNote.resource_id.is_not(None)).values(size=None))
    conn.execute(
        update(Resource)
        .where(Resource.ext.in_(NOTE_EXTENSIONS), Resource.ingested_at.is_not(None))
        .values(ingested_at=None)
    )


# --- the user's key aliases (#411) ---


def check_alias(key: str, field: str) -> tuple[str, str]:
    """An alias as it is listed (both stripped), or :class:`NoteKeyError`: the key can't be
    one Tagalot reads itself (``title``, ``tags``, ``cover``, ``tagalot-…``)."""
    key, field = key.strip(), field.strip()
    if not key:
        raise NoteKeyError("Type the key to read as a field")
    if not field:
        raise NoteKeyError(f"Name the field {key!r} fills")
    folded = key.casefold()
    if folded.startswith(RESERVED_PREFIX):
        raise NoteKeyError(f"Keys starting with {RESERVED_PREFIX!r} are Tagalot's own")
    if folded in NOT_ALIASED:
        raise NoteKeyError(f"Tagalot reads {key!r} itself")
    return key, field


def key_aliases(*lists: Mapping[str, str]) -> dict[str, str]:
    """The aliases to read with, by case-folded key: those of ``lists`` (the user's, then
    the keep's), a later list's winning for the same key."""
    found: dict[str, str] = {}
    for aliases in lists:
        for key, field in aliases.items():
            try:
                key, field = check_alias(key, field)
            except NoteKeyError:
                continue
            found[key.casefold()] = field
    return found


def parse_alias_lines(lines: Iterable[str]) -> dict[str, str]:
    """Aliases as typed in Keep configuration, ``key = field`` a line (blank lines skipped;
    a key given again, case aside, takes its last field), or :class:`NoteKeyError`."""
    aliases: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        key, equals, field = line.partition("=")
        if not equals:
            raise NoteKeyError(f"Write {line.strip()!r} as key = field (synopsis = Description)")
        key, field = check_alias(key, field)
        aliases = with_alias(aliases, key, field)
    return aliases


def alias_lines(aliases: Mapping[str, str]) -> str:
    """Aliases as Keep configuration shows them, ``key = field`` a line."""
    return "\n".join(f"{key} = {field}" for key, field in aliases.items())


def with_alias(aliases: Mapping[str, str], key: str, field: str | None) -> dict[str, str]:
    """``aliases`` with ``key`` reading as ``field`` (at the end, unless listed; in its
    place, if so), or without it (``None``), case aside."""
    folded = key.strip().casefold()
    listed = {k: v for k, v in aliases.items() if k.strip().casefold() != folded or field}
    if field is None:
        return listed
    for k in listed:
        if k.strip().casefold() == folded:
            listed[k] = field.strip()
            return listed
    return {**listed, key.strip(): field.strip()}


def sync_aliases(conn: Connection, aliases: Mapping[str, str]) -> bool:
    """Remember ``aliases`` (from :func:`key_aliases`) as the ones notes and Markdown files
    are read with; when they differ from the last ones, mark every note and Markdown file to
    be read again (a key now aliased fills its field, one no longer aliased is an Extra
    field again). Returns whether they differed."""
    if applied_aliases(conn) == dict(aliases):
        return False
    _read_again(conn)
    conn.execute(delete(NoteKeyAlias))
    if aliases:
        conn.execute(
            sqlite_insert(NoteKeyAlias),
            [{"key": k, "field": v} for k, v in sorted(aliases.items())],
        )
    return True


@dataclass(frozen=True)
class NoteKeyChange:
    """Everything needed to undo and redo ignoring a key in every note."""

    label: str
    key: str
    """As the user listed it."""
    removed: tuple[tuple[int, str, str, FieldSource], ...]
    """``(entity, Extra field name, value, provenance)``: the fields notes and files gave
    that it removed."""
    listed: bool = False
    """The keep's list had the key already, so undoing leaves it there."""


def ignore_note_key(
    conn: Connection, schema: ThemeSchema, key: str, *, listed: bool = False
) -> NoteKeyChange:
    """Ignore ``key`` in every note and file: remove the Extra fields notes and files gave
    under it (a DB writer job). The caller adds it to ``keep.toml``."""
    key = check_key(key)
    folded = key.casefold()
    found = conn.execute(
        select(
            FieldProvenance.entity_id, FieldProvenance.field, FieldProvenance.source, Entity.extra
        )
        .join(Entity, Entity.id == FieldProvenance.entity_id)
        .where(
            FieldProvenance.field.startswith(EXTRA, autoescape=True),
            FieldProvenance.source.in_([FieldSource.NOTE, FieldSource.EXTRACTED]),
        )
    ).all()
    removed = tuple(
        (entity_id, name, str(extra[name]), source)
        for entity_id, field_name, source, extra in found
        if (name := field_name[len(EXTRA) :]).casefold() == folded and name in (extra or {})
    )
    items = len({entity_id for entity_id, _, _, _ in removed})
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
    sources: dict[tuple[int, str], FieldSource] = {}
    for entity_id, name, value, source in change.removed:
        by_entity.setdefault(entity_id, {})[name] = value
        sources[entity_id, name] = source
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
                        "source": sources[entity_id, n],
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
