"""The keep's notes folder (DESIGN.md §9 *The notes folder*, #382).

One folder per keep (``[notes] root`` and ``path`` in ``keep.toml``) holds notes for items
that have no folder of their own: actors, authors, a music artist. The scanner records only
its Markdown files and keeps all of it from the theme; after the roots are scanned,
:func:`plan_folder_notes` (in a worker) reads the notes that changed and matches every note
to an item, and :func:`apply_folder_notes` (a DB writer job) gives each matched item its
note, with the same rules as a folder note's (``core.notes.apply_note``).

- **Type** (:func:`note_type`): ``tagalot-type`` (a type's name or label, singular or
  plural, ignoring case and accents), else the note's folder's name the same way, else any
  type. The key wins.
- **Item:** the note's title (``title``, else its file name) or one of its ``aliases``
  equals an item's title, ignoring case and accents. A note matching one item is its note,
  unless the item has a folder note (which wins) or an earlier note, by path, matched it
  first. The rest are listed in Triage (``notes_folder_note``).
- **Offline isn't gone:** an offline note keeps its item; a missing one lets it go.
"""

import logging
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from tagalot.core.ingest import IngestSession, IngestWarning
from tagalot.core.models import (
    Entity,
    EntityNote,
    NoteIgnoredKey,
    NotesFolderNote,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from tagalot.core.notes import (
    DEFAULT_IGNORED_KEYS,
    NOTE_EXTENSIONS,
    NoteFile,
    NoteText,
    apply_note,
    as_text,
    map_note,
    primary_files,
    read_note,
)
from tagalot.core.roots import local_path
from tagalot.core.scanner import in_notes
from tagalot.core.tags import search_key
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import EntityRef, entity_label, entity_plural

logger = logging.getLogger(__name__)

TYPE_KEY = "tagalot-type"


@dataclass(frozen=True)
class FolderNoteMeta:
    """What a notes-folder note is matched by."""

    title: str
    aliases: tuple[str, ...] = ()
    type: str | None = None


def type_names(schema: ThemeSchema) -> dict[str, str]:
    """Each way of naming a type (its class name, label, and plural, folded), to its id."""
    names: dict[str, str] = {}
    for entity, table in schema.entities.items():
        for name in (entity.__name__, entity_label(entity), entity_plural(entity)):
            names.setdefault(search_key(name), table.type_id)
        names.setdefault(search_key(table.type_id.rpartition(".")[2]), table.type_id)
    return names


def note_type(
    names: Mapping[str, str], value: object, folder: str
) -> tuple[str | None, str | None]:
    """The type a note is for, from its ``tagalot-type`` ``value`` (if any), else the name of
    its ``folder`` (``""``: the notes folder itself); with a problem to report when the key
    names no type."""
    problem = None
    if value is not None:
        text = as_text(value)
        if text and (found := names.get(search_key(text))) is not None:
            return found, None
        problem = f"{TYPE_KEY}: {text!r} is no type of this keep"
    if folder:
        return names.get(search_key(folder.rpartition("/")[2])), problem
    return None, problem


def note_meta(
    note: NoteText, relpath: str, notes_path: str, names: Mapping[str, str]
) -> tuple[FolderNoteMeta, list[str]]:
    """What a notes-folder note at ``relpath`` (in a notes folder at ``notes_path``) is
    matched by, and problems to report."""
    front = {str(k).strip().casefold(): v for k, v in note.front.items()}
    stem = posixpath.splitext(posixpath.basename(relpath))[0]
    title = as_text(front.get("title")) or stem
    raw = front.get("aliases")
    aliases = raw if isinstance(raw, list | tuple) else [raw] if raw is not None else []
    folder = posixpath.dirname(relpath)
    inside = folder[len(notes_path) :].strip("/") if notes_path else folder
    type_id, problem = note_type(names, front.get(TYPE_KEY), inside)
    meta = FolderNoteMeta(
        title, tuple(a for x in aliases if (a := as_text(x)) is not None), type_id
    )
    return meta, [problem] if problem else []


def _keys(meta: FolderNoteMeta) -> set[str]:
    return {k for k in (search_key(t) for t in (meta.title, *meta.aliases)) if k}


@dataclass(frozen=True)
class FolderNoteRow:
    """A note's ``notes_folder_note`` row to be."""

    file: NoteFile
    meta: FolderNoteMeta
    matches: int
    item_id: int | None
    applied: bool


@dataclass
class FolderNotesPlan:
    """What :func:`apply_folder_notes` writes, worked out by :func:`plan_folder_notes`."""

    root_id: str
    rows: list[FolderNoteRow] = field(default_factory=list)
    drop: list[int] = field(default_factory=list)
    """Notes no longer there (missing, skipped, outside the folder): their rows go."""
    give: dict[int, tuple[NoteFile, NoteText]] = field(default_factory=dict)
    """Item -> the note it now has, read."""
    take: list[int] = field(default_factory=list)
    """Items whose notes-folder note goes."""
    warnings: list[IngestWarning] = field(default_factory=list)


def plan_folder_notes(
    reader: Engine, schema: ThemeSchema, root_id: str, notes_path: str, path: str
) -> FolderNotesPlan:
    """Read the notes folder's notes that changed and match every note to an item (in the
    caller's thread: a worker's). ``path`` is this computer's path for the root."""
    plan = FolderNotesPlan(root_id)
    with reader.connect() as conn:
        files = [
            (NoteFile(rid, rel, size, mtime), status)
            for rid, rel, size, mtime, status in conn.execute(
                select(
                    Resource.id, Resource.relpath, Resource.size, Resource.mtime_ns, Resource.status
                ).where(
                    Resource.root_id == root_id,
                    Resource.kind == ResourceKind.FILE,
                    Resource.ext.in_(NOTE_EXTENSIONS),
                    Resource.skipped.is_(False),
                    Resource.parent_resource_id.is_(None),
                )
            )
            if in_notes(rel, notes_path)
        ]
        stored = {row.resource_id: row for row in conn.execute(select(NotesFolderNote.__table__))}
        notes = {
            entity_id: (resource_id, size, mtime)
            for entity_id, resource_id, size, mtime in conn.execute(
                select(
                    EntityNote.entity_id,
                    EntityNote.resource_id,
                    EntityNote.size,
                    EntityNote.mtime_ns,
                ).where(EntityNote.resource_id.is_not(None))
            )
        }
        items = conn.execute(select(Entity.id, Entity.type, Entity.title)).all()
    names = type_names(schema)
    present = {f.resource_id for f, status in files if status != ResourceStatus.MISSING}
    plan.drop = [rid for rid in stored if rid not in present]
    offline = {f.resource_id for f, status in files if status == ResourceStatus.OFFLINE}
    texts: dict[int, NoteText] = {}
    metas: dict[int, FolderNoteMeta] = {}
    by_id = {f.resource_id: f for f, _ in files}

    def read(file: NoteFile) -> NoteText | None:
        try:
            return read_note(local_path(path, file.relpath))
        except (OSError, ValueError) as e:
            plan.warnings.append(
                IngestWarning(root_id, file.relpath, f"Couldn't read this note: {e}")
            )
            logger.warning("Cannot read note %s in root %s: %s", file.relpath, root_id, e)
            return None

    for file, status in files:
        if status == ResourceStatus.MISSING:
            continue  # gone: its item lets it go
        row = stored.get(file.resource_id)
        if row is not None and (
            status != ResourceStatus.OK or (row.size, row.mtime_ns) == (file.size, file.mtime_ns)
        ):
            metas[file.resource_id] = FolderNoteMeta(row.title, tuple(row.aliases), row.type)
            continue
        if status != ResourceStatus.OK:
            continue
        text = read(file)
        if text is None:
            if row is not None:
                metas[file.resource_id] = FolderNoteMeta(row.title, tuple(row.aliases), row.type)
            continue
        texts[file.resource_id] = text
        metas[file.resource_id], problems = note_meta(text, file.relpath, notes_path, names)
        plan.warnings += [IngestWarning(root_id, file.relpath, p) for p in problems]

    # Matching: titles and aliases against items' titles, ignoring case and accents.
    index: dict[str, list[tuple[int, str]]] = {}
    for entity_id, type_id, title in items:
        index.setdefault(search_key(title), []).append((entity_id, type_id))
    folder_notes = {e for e, (rid, _, _) in notes.items() if rid not in by_id}
    current = {e: rid for e, (rid, _, _) in notes.items() if rid in by_id}
    matched: dict[int, list[int]] = {}
    for rid, meta in metas.items():
        hits = {
            entity_id
            for key in _keys(meta)
            for entity_id, type_id in index.get(key, ())
            if meta.type is None or type_id == meta.type
        }
        matched[rid] = sorted(hits)
    # An offline note keeps its item; otherwise the first note (by path) matching an item
    # that has no folder note is its note.
    owner: dict[int, int] = {e: rid for e, rid in current.items() if rid in offline}
    for rid in sorted(metas, key=lambda r: by_id[r].relpath.casefold()):
        found = matched[rid]
        if rid in offline or len(found) != 1:
            continue
        item = found[0]
        if item not in folder_notes and item not in owner:
            owner[item] = rid
    applied = {rid: item for item, rid in owner.items()}
    for rid, meta in metas.items():
        found = matched[rid]
        one = applied.get(rid, found[0] if len(found) == 1 else None)
        plan.rows.append(FolderNoteRow(by_id[rid], meta, len(found), one, rid in applied))

    for item, rid in owner.items():
        file = by_id[rid]
        if rid in offline or notes.get(item) == (rid, file.size, file.mtime_ns):
            continue  # it has this note, as it is
        text = texts.get(rid) or read(file)
        if text is not None:
            plan.give[item] = (file, text)
    plan.take = [item for item, rid in current.items() if item not in owner]
    return plan


def apply_folder_notes(
    conn: Connection, schema: ThemeSchema, plan: FolderNotesPlan
) -> tuple[list[int], list[IngestWarning]]:
    """Give items their notes-folder notes, and take away those that went (a DB writer
    job); returns the resources to read again (as :func:`~tagalot.core.notes.apply_notes`)
    and values that were left out."""
    if plan.drop:
        conn.execute(delete(NotesFolderNote).where(NotesFolderNote.resource_id.in_(plan.drop)))
    for row in plan.rows:
        fields = {
            "resource_id": row.file.resource_id,
            "size": row.file.size,
            "mtime_ns": row.file.mtime_ns,
            "title": row.meta.title,
            "aliases": list(row.meta.aliases),
            "type": row.meta.type,
            "matches": row.matches,
            "item_id": row.item_id,
            "applied": row.applied,
        }
        upsert = sqlite_insert(NotesFolderNote).values(fields)
        conn.execute(
            upsert.on_conflict_do_update(
                index_elements=["resource_id"],
                set_={k: upsert.excluded[k] for k in fields if k != "resource_id"},
            )
        )
    ctx = IngestSession(conn, schema)
    ignored = set(conn.scalars(select(NoteIgnoredKey.key))) or DEFAULT_IGNORED_KEYS
    types = (
        dict(conn.execute(select(Entity.id, Entity.type)).all()) if plan.give or plan.take else {}
    )
    folders = dict(conn.execute(select(EntityNote.entity_id, EntityNote.folder_id)).all())
    reread: list[int] = []
    warnings: list[IngestWarning] = []
    changes = [(item, plan.give.get(item)) for item in [*plan.give, *plan.take]]
    for item, given in changes:
        type_id = types.get(item)
        if type_id is None:
            continue
        entity = EntityRef(item, type_id)
        file, values = None, None
        if given is not None:
            file, text = given
            values = map_note(text, schema.by_type_id(type_id).fields, ignored)
            warnings += [
                IngestWarning(plan.root_id, file.relpath, f"A note's value was left out: {p}")
                for p in values.problems
            ]
        if apply_note(ctx, entity, values, file, folder_id=folders.get(item), folder_note=False):
            reread += primary_files(conn, schema, entity)
    ctx.flush()
    return reread, warnings


def notes_folder_files(conn: Connection) -> set[int]:
    """The notes folder's notes (their resources)."""
    return set(conn.scalars(select(NotesFolderNote.resource_id)))
