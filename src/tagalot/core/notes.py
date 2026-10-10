"""Notes: Markdown files that describe items (DESIGN.md §9 *Notes*, #379).

- **Finding them:** an item whose primary file is a folder takes the note inside it,
  ``<folder name>.md``, else ``index.md``, else ``README.md`` (each also ``.markdown``, case
  aside: :func:`note_rank`); a theme names a folder for another item with ``ctx.note``. The
  scanner records note-named files in the folders the theme records, whatever its
  extensions, and a scan keeps them from the theme when it doesn't take Markdown.
- **Reading** (:func:`read_notes`, in the scan's thread): only notes that changed since they
  were last read (their file, size, and time against ``entity_note``).
- **Mapping** (:func:`map_note`): ``title``; ``tags``/``keywords`` as file keywords; a key
  matching a field's name or label (case, spaces, ``_`` and ``-`` aside) as that field,
  converted to its type; ``tagalot-…`` keys, ``aliases``, and ignored keys skipped; the
  rest as Extra fields, as text.
- **Applying** (:func:`apply_note`, in the DB writer): provenance ``note`` ranks below the
  theme's own reading of files and above online details; a folder note's title wins over
  the folder's name; the user's edits always win. A value the note no longer gives is
  cleared, and the item's files are read again so they can fill it.
"""

import datetime
import logging
import posixpath
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from tagalot.core.ingest import TITLE, IngestSession, IngestWarning
from tagalot.core.models import (
    Entity,
    EntityNote,
    EntityResource,
    FieldProvenance,
    FieldSource,
    NoteKeyRemoval,
    Resource,
    ResourceKind,
    ResourceStatus,
    utcnow,
)
from tagalot.core.roots import local_path
from tagalot.core.theme_schema import EntityTable, ThemeSchema
from tagalot.themes.api import EntityRef, FieldInfo, Kind
from tagalot.themes.readers import split_front_matter, split_keywords

logger = logging.getLogger(__name__)

NOTE_EXTENSIONS = (".md", ".markdown")
NOTE_NAMES = ("index", "readme")
"""A folder's note is named after the folder, else one of these, in this order."""
DEFAULT_IGNORED_KEYS = frozenset({"cssclasses", "cssclass", "publish", "permalink", "position"})
"""Keys editors add for themselves, never Extra fields (§9 *Notes*: built-in defaults)."""
RESERVED_PREFIX = "tagalot-"
"""Keys Tagalot reads itself (``tagalot-type``): never Extra fields."""
MAX_NOTE = 1024 * 1024
"""Bytes of a note read; the rest of a longer one is left out."""
EXTRA = "extra:"
"""Provenance rows for an Extra field are named ``extra:<name>``."""


def note_rank(relpath: str) -> int | None:
    """How good a note for its folder this file is (0 named after the folder, 1 ``index``, 2
    ``README``), or ``None`` when it isn't named as a note."""
    folder, _, name = relpath.rpartition("/")
    stem, ext = posixpath.splitext(name)
    if ext.lower() not in NOTE_EXTENSIONS:
        return None
    stem = stem.casefold()
    if folder and stem == folder.rpartition("/")[2].casefold():
        return 0
    if stem in NOTE_NAMES:
        return 1 + NOTE_NAMES.index(stem)
    return None


# --- reading ---


@dataclass(frozen=True)
class NoteText:
    """A note file as read: its front matter's keys and values, and its body."""

    front: Mapping[str, Any]
    body: str


def read_note(path: str) -> NoteText:
    """Read a note; ``OSError`` if it can't be read, ``ValueError`` if its front matter
    doesn't parse."""
    with open(path, "rb") as file:
        raw = file.read(MAX_NOTE)
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    front, body = split_front_matter(text)
    return NoteText(dict(front or {}), body.strip("\n"))


# --- mapping ---


@dataclass
class NoteValues:
    """What a note gives an item, before ranking against what it has."""

    title: str | None = None
    keywords: list[str] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)
    """Field name -> value, converted to the field's type."""
    extra: dict[str, str] = field(default_factory=dict)
    """Extra field name (as the note writes it) -> text."""
    body: str = ""
    problems: list[str] = field(default_factory=list)


def _key(text: str) -> str:
    return re.sub(r"[\s_\-]+", "", text).casefold()


def map_note(
    note: NoteText, fields: Sequence[FieldInfo], ignored: Collection[str] = ()
) -> NoteValues:
    """What ``note`` gives an item with these fields; ``ignored`` keys (and the built-in
    ones) are left out."""
    by_key: dict[str, FieldInfo] = {}
    for info in fields:
        by_key.setdefault(_key(info.name), info)
        by_key.setdefault(_key(info.spec.label), info)
    skip = DEFAULT_IGNORED_KEYS | {k.casefold() for k in ignored}
    found = NoteValues(body=note.body)
    for raw_key, value in note.front.items():
        key = str(raw_key).strip()
        folded = key.casefold()
        if not key or folded.startswith(RESERVED_PREFIX) or folded == "aliases" or folded in skip:
            continue
        if value is None or value == "" or value == []:
            continue
        if folded == "title":
            found.title = as_text(value)
        elif folded in ("tags", "keywords"):
            found.keywords += split_keywords(value)
        elif (target := by_key.get(_key(key))) is not None:
            try:
                found.fields[target.name] = convert(value, target.type)
            except ValueError as e:
                found.problems.append(f"{key}: {e}")
        elif (text := as_text(value)) is not None:
            found.extra[key] = text
    return found


def as_text(value: Any) -> str | None:
    """A front-matter value as an Extra field's text: lists joined with ``, ``, dates as
    ``YYYY-MM-DD`` (and times as ``HH:MM``), yes/no as ``true``/``false``; ``None`` for a
    table of keys, which isn't kept."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, int | float | str):
        return " ".join(str(value).split()) or None
    if isinstance(value, list | tuple):
        parts = [t for item in value if (t := as_text(item)) is not None]
        return ", ".join(parts) or None
    return None


def convert(value: Any, kind: type) -> Any:
    """``value`` as a field of type ``kind``; ``ValueError`` when it isn't one."""
    if kind is str:
        text = as_text(value)
        if text is None:
            raise ValueError("isn't text")
        return text
    if kind is bool:
        if isinstance(value, bool):
            return value
        words = {"true": True, "yes": True, "false": False, "no": False}
        if isinstance(value, str) and value.strip().casefold() in words:
            return words[value.strip().casefold()]
        raise ValueError("isn't yes or no")
    if isinstance(value, bool):
        raise ValueError(f"isn't a {_names[kind]}")
    if kind is int:
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"\s*-?[\d,]+\s*", value):
            return int(value.replace(",", ""))
        raise ValueError("isn't a whole number")
    if kind is float:
        if isinstance(value, int | float):
            return float(value)
        try:
            return float(str(value).replace(",", ""))
        except ValueError:
            raise ValueError("isn't a number") from None
    if kind is datetime.datetime:
        when = value
        if isinstance(value, str):
            try:
                when = datetime.datetime.fromisoformat(value.strip())
            except ValueError:
                raise ValueError("isn't a date and time") from None
        elif isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
            when = datetime.datetime(value.year, value.month, value.day)
        if not isinstance(when, datetime.datetime):
            raise ValueError("isn't a date and time")
        return (when if when.tzinfo else when.astimezone()).astimezone(datetime.UTC)
    if kind is datetime.date:
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        try:
            return datetime.date.fromisoformat(str(value).strip())
        except ValueError:
            raise ValueError("isn't a date (YYYY-MM-DD)") from None
    raise ValueError(f"can't be read as {kind.__name__}")


_names = {int: "whole number", float: "number", datetime.date: "date", datetime.datetime: "date"}


def body_without_title(body: str, title: str | None) -> str:
    """``body`` without a first ``# heading`` that repeats the item's title."""
    lines = body.split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None and title:
        heading = re.fullmatch(r"\s*#\s+(.*?)\s*#*\s*", lines[first])
        if heading and heading.group(1).casefold() == title.strip().casefold():
            return "\n".join(lines[first + 1 :]).strip("\n")
    return body


# --- applying (in the DB writer) ---


def _empty(value: Any) -> bool:
    return value is None or value == ""


def _set_source(conn: Connection, entity_id: int, name: str, source: FieldSource) -> None:
    row = {"entity_id": entity_id, "field": name, "source": source, "updated_at": utcnow()}
    upsert = sqlite_insert(FieldProvenance).values(row)
    conn.execute(
        upsert.on_conflict_do_update(
            index_elements=["entity_id", "field"],
            set_={"source": upsert.excluded.source, "updated_at": upsert.excluded.updated_at},
        )
    )


def _drop_source(conn: Connection, entity_id: int, names: Iterable[str]) -> None:
    names = list(names)
    if names:
        conn.execute(
            delete(FieldProvenance).where(
                FieldProvenance.entity_id == entity_id, FieldProvenance.field.in_(names)
            )
        )


@dataclass(frozen=True)
class NoteFile:
    """The file a note was read from, as it was then."""

    resource_id: int
    relpath: str
    size: int | None
    mtime_ns: int | None


def apply_note(
    ctx: IngestSession,
    entity: EntityRef,
    values: NoteValues | None,
    note: NoteFile | None,
    *,
    folder_id: int | None,
    folder_note: bool,
) -> bool:
    """Give ``entity`` what its note says (``None``: it has none now), replacing what an
    earlier note gave; returns whether its files should be read again, because the note no
    longer gives a title or field it gave before."""
    conn = ctx.conn
    entity_table = ctx.schema.by_type_id(entity.type)
    row = conn.execute(
        select(Entity.title, Entity.extra).where(Entity.id == entity.id)
    ).one_or_none()
    if row is None:
        return False
    title, extra = row
    given = values or NoteValues()
    sources = dict(
        conn.execute(
            select(FieldProvenance.field, FieldProvenance.source).where(
                FieldProvenance.entity_id == entity.id
            )
        ).all()
    )
    reread = False
    changed = False

    # The title: a folder note's wins over the folder's name, never over the user's.
    if folder_note and given.title and sources.get(TITLE) != FieldSource.USER:
        if title != given.title:
            conn.execute(update(Entity).where(Entity.id == entity.id).values(title=given.title))
            title, changed = given.title, True
        if sources.get(TITLE) != FieldSource.NOTE:
            _set_source(conn, entity.id, TITLE, FieldSource.NOTE)
    elif sources.get(TITLE) == FieldSource.NOTE:
        _drop_source(conn, entity.id, [TITLE])  # the files' name comes back on re-reading
        reread = True

    # Fields: filled when no file gave them; online details give way; the user's stay.
    table = entity_table.table
    current = dict(conn.execute(select(table).where(table.c.id == entity.id)).one()._mapping)
    nullable = {f.name for f in entity_table.fields if f.nullable}
    writes: dict[str, Any] = {}
    for name, value in given.fields.items():
        source = sources.get(name)
        free = source in (None, FieldSource.NOTE, FieldSource.FETCHED) or (
            source == FieldSource.EXTRACTED and _empty(current.get(name))
        )
        if not free:
            continue
        if current.get(name) != value:
            writes[name] = value
        if source != FieldSource.NOTE:
            _set_source(conn, entity.id, name, FieldSource.NOTE)
    dropped = [
        name
        for name, source in sources.items()
        if source == FieldSource.NOTE
        and name != TITLE
        and not name.startswith(EXTRA)
        and name not in given.fields
        and name in current
    ]
    for name in dropped:
        if name in nullable:
            writes[name] = None
    if dropped:
        _drop_source(conn, entity.id, dropped)
        reread = True
    if writes:
        conn.execute(update(table).where(table.c.id == entity.id).values(**writes))
        changed = True

    # Extra fields: the note's, unless the user removed or edited them.
    extras = dict(extra or {})
    removed = set(
        conn.scalars(select(NoteKeyRemoval.key).where(NoteKeyRemoval.entity_id == entity.id))
    )
    theirs = {
        name[len(EXTRA) :]: source for name, source in sources.items() if name.startswith(EXTRA)
    }
    for name, text in given.extra.items():
        if name.casefold() in removed or theirs.get(name) == FieldSource.USER:
            continue
        if name in extras and name not in theirs:
            continue  # the user's own field of that name
        if extras.get(name) != text:
            extras[name] = text
        if theirs.get(name) != FieldSource.NOTE:
            _set_source(conn, entity.id, EXTRA + name, FieldSource.NOTE)
    gone = [
        name
        for name, source in theirs.items()
        if source == FieldSource.NOTE and (name not in given.extra or name.casefold() in removed)
    ]
    for name in gone:
        extras.pop(name, None)
    _drop_source(conn, entity.id, (EXTRA + name for name in gone))
    if extras != (extra or {}):
        conn.execute(update(Entity).where(Entity.id == entity.id).values(extra=extras))
        changed = True

    # Keywords, from the note file; the body; where the note was read from.
    stored = conn.execute(
        select(EntityNote.resource_id, EntityNote.body).where(EntityNote.entity_id == entity.id)
    ).one_or_none()
    old_file = stored[0] if stored is not None else None
    if old_file is not None and (note is None or note.resource_id != old_file):
        ctx.keywords(entity, old_file, [])
    if note is not None:
        ctx.keywords(entity, note.resource_id, given.keywords)
    body = body_without_title(given.body, title) if note is not None else None
    if note is None and not folder_note and folder_id is not None:
        keep_row = True  # a theme names the folder: keep looking there
    else:
        keep_row = note is not None
    if keep_row:
        upsert = sqlite_insert(EntityNote).values(
            entity_id=entity.id,
            folder_id=folder_id,
            resource_id=note.resource_id if note else None,
            size=note.size if note else None,
            mtime_ns=note.mtime_ns if note else None,
            body=body or None,
            updated_at=utcnow(),
        )
        conn.execute(
            upsert.on_conflict_do_update(
                index_elements=["entity_id"],
                set_={
                    c: upsert.excluded[c]
                    for c in ("folder_id", "resource_id", "size", "mtime_ns", "body", "updated_at")
                },
            )
        )
    elif stored is not None:
        conn.execute(delete(EntityNote).where(EntityNote.entity_id == entity.id))
    if (stored[1] if stored is not None else None) != (body or None):
        changed = True
    if changed:
        ctx.mark_changed(entity.id)
    return reread


# --- a root's notes, at a scan ---


@dataclass(frozen=True)
class NoteSite:
    """An item whose note is looked for in a folder."""

    entity: EntityRef
    folder_id: int
    folder: str
    """The folder's root-relative path."""
    folder_note: bool
    """The folder is the item's own (its primary file), so the note's title wins."""


@dataclass(frozen=True)
class NoteChange:
    """An item whose note is new, changed, or gone since it was last read."""

    site: NoteSite
    file: NoteFile | None
    text: NoteText | None


def note_sites(conn: Connection, schema: ThemeSchema, root_id: str) -> list[NoteSite]:
    """The root's items that take a note: those whose primary file is a folder, and those
    a theme named a folder for (``ctx.note``)."""
    sites: dict[int, NoteSite] = {}
    for entity_table in schema.entities.values():
        primary = next((r for r in entity_table.entity.roles if r.primary), None)
        if primary is None or primary.kinds != {Kind.DIR}:
            continue
        rows = conn.execute(
            select(EntityResource.entity_id, Resource.id, Resource.relpath)
            .join(Resource, Resource.id == EntityResource.resource_id)
            .join(Entity, Entity.id == EntityResource.entity_id)
            .where(
                Entity.type == entity_table.type_id,
                EntityResource.role == primary.name,
                Resource.root_id == root_id,
                Resource.kind == ResourceKind.DIR,
                Resource.status == ResourceStatus.OK,
            )
        )
        for entity_id, folder_id, folder in rows:
            ref = EntityRef(entity_id, entity_table.type_id)
            sites[entity_id] = NoteSite(ref, folder_id, folder, True)
    named = conn.execute(
        select(EntityNote.entity_id, Entity.type, Resource.id, Resource.relpath)
        .join(Resource, Resource.id == EntityNote.folder_id)
        .join(Entity, Entity.id == EntityNote.entity_id)
        .where(Resource.root_id == root_id, Resource.status == ResourceStatus.OK)
    )
    for entity_id, type_id, folder_id, folder in named:
        if entity_id not in sites:
            sites[entity_id] = NoteSite(EntityRef(entity_id, type_id), folder_id, folder, False)
    return list(sites.values())


def note_files(conn: Connection, root_id: str) -> dict[str, NoteFile]:
    """Each folder's note in the root: the best-named one (a ``.md`` before a
    ``.markdown`` of the same name), by folder path."""
    best: dict[str, tuple[tuple[int, bool, str], NoteFile]] = {}
    rows = conn.execute(
        select(Resource.id, Resource.relpath, Resource.size, Resource.mtime_ns).where(
            Resource.root_id == root_id,
            Resource.kind == ResourceKind.FILE,
            Resource.ext.in_(NOTE_EXTENSIONS),
            Resource.status == ResourceStatus.OK,
            Resource.skipped.is_(False),
            Resource.parent_resource_id.is_(None),
        )
    )
    for resource_id, relpath, size, mtime in rows:
        rank = note_rank(relpath)
        if rank is None:
            continue
        folder = relpath.rpartition("/")[0]
        order = (rank, not relpath.lower().endswith(".md"), relpath)
        if folder not in best or order < best[folder][0]:
            best[folder] = (order, NoteFile(resource_id, relpath, size, mtime))
    return {folder: file for folder, (_, file) in best.items()}


def used_notes(conn: Connection) -> set[int]:
    """Files that are items' notes."""
    return set(
        conn.scalars(select(EntityNote.resource_id).where(EntityNote.resource_id.is_not(None)))
    )


def read_notes(
    reader: Engine, schema: ThemeSchema, root_id: str, path: str
) -> tuple[list[NoteChange], list[IngestWarning]]:
    """The root's notes that changed since they were last read, read (in the caller's
    thread: a worker's, never the DB writer's). A note that can't be read is reported and
    left as it was."""
    with reader.connect() as conn:
        sites = note_sites(conn, schema, root_id)
        files = note_files(conn, root_id)
        stored = {
            entity_id: (resource_id, size, mtime)
            for entity_id, resource_id, size, mtime in conn.execute(
                select(
                    EntityNote.entity_id,
                    EntityNote.resource_id,
                    EntityNote.size,
                    EntityNote.mtime_ns,
                ).where(EntityNote.entity_id.in_([s.entity.id for s in sites]))
            )
        }
    changes: list[NoteChange] = []
    warnings: list[IngestWarning] = []
    for site in sites:
        file = files.get(site.folder)
        was = stored.get(site.entity.id)
        if file is None:
            if was is not None and was[0] is not None:
                changes.append(NoteChange(site, None, None))
            elif was is None and not site.folder_note:
                changes.append(NoteChange(site, None, None))  # a folder a theme just named
            continue
        if was == (file.resource_id, file.size, file.mtime_ns):
            continue
        try:
            text = read_note(local_path(path, file.relpath))
        except (OSError, ValueError) as e:
            warnings.append(IngestWarning(root_id, file.relpath, f"Couldn't read this note: {e}"))
            logger.warning("Cannot read note %s in root %s: %s", file.relpath, root_id, e)
            continue
        changes.append(NoteChange(site, file, text))
    return changes, warnings


def apply_notes(
    conn: Connection, schema: ThemeSchema, root_id: str, changes: Sequence[NoteChange]
) -> tuple[list[int], list[IngestWarning]]:
    """Apply notes read by :func:`read_notes` (a DB writer job); returns the resources to
    read again (the primary files of items whose notes stopped giving a value) and
    problems with the notes' values."""
    ctx = IngestSession(conn, schema)
    reread: list[int] = []
    warnings: list[IngestWarning] = []
    for change in changes:
        site = change.site
        values = None
        if change.text is not None:
            fields = schema.by_type_id(site.entity.type).fields
            values = map_note(change.text, fields)
            relpath = change.file.relpath if change.file else None
            warnings += [
                IngestWarning(root_id, relpath, f"A note's value was left out: {problem}")
                for problem in values.problems
            ]
        if apply_note(
            ctx,
            site.entity,
            values,
            change.file,
            folder_id=site.folder_id,
            folder_note=site.folder_note,
        ):
            reread += primary_files(conn, schema, site.entity)
    ctx.flush()
    return reread, warnings


def primary_files(conn: Connection, schema: ThemeSchema, entity: EntityRef) -> list[int]:
    """The resources in an item's primary role (read again to restore what a note gave)."""
    entity_table: EntityTable = schema.by_type_id(entity.type)
    primary = next((r for r in entity_table.entity.roles if r.primary), None)
    if primary is None:
        return []
    return list(
        conn.scalars(
            select(EntityResource.resource_id).where(
                EntityResource.entity_id == entity.id, EntityResource.role == primary.name
            )
        )
    )
