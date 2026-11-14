"""The keep's notes folder (DESIGN.md §9 *The notes folder*, #382).

One folder per keep (``[notes] root`` and ``path`` in ``keep.toml``) holds notes for items
that have no folder of their own: actors, authors, a music artist. The scanner records only
its Markdown files and keeps all of it from the theme; after the roots are scanned,
:func:`plan_folder_notes` (in a worker) reads the notes that changed and matches every note
to an item, and :func:`apply_folder_notes` (a DB writer job) gives each matched item its
note, with the same rules as a folder note's (``core.notes.apply_note``).

- **Type** (:func:`note_type`): ``tagalot-type`` (a type's name or label, singular or
  plural, ignoring case and accents), else the outermost folder above the note (inside
  the notes folder) named the same way, so everything in a type's folder is that type's,
  however deep (``People/Family/Alice.md``, #656), else any type. The key wins.
- **Item:** the note's title (``title``, else its file name) or one of its ``aliases``
  equals an item's title, ignoring case and accents; or, when it gives a ``path``
  (#658), the item's folder or file path ends in that path (:func:`path_matches`), its
  title aside. A note matching one item is its note,
  unless the item has a folder note (which wins) or an earlier note, by path, matched it
  first. The rest are listed in Triage (``notes_folder_note``).
- **Renaming** (#662): a note that can find its item another way, by ``path`` or one
  of its ``aliases``, names it with its ``title`` (as a folder note does; never over
  the user's). Without either, the title only finds the item.
- **Offline isn't gone:** an offline note keeps its item; a missing one lets it go.
"""

import logging
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from tagalot.core.extra_keys import DEFAULT_IGNORED_KEYS, applied_aliases, as_text
from tagalot.core.ingest import IngestSession, IngestWarning
from tagalot.core.models import (
    Entity,
    EntityNote,
    EntityResource,
    NoteIgnoredKey,
    NotesFolderNote,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from tagalot.core.notes import (
    NOTE_EXTENSIONS,
    NoteFile,
    NoteText,
    apply_note,
    map_note,
    naming_files,
    read_note,
)
from tagalot.core.own_notes import applied_own_fields
from tagalot.core.roots import local_path
from tagalot.core.scanner import in_notes
from tagalot.core.tags import search_key
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import EntityRef, entity_label, entity_plural, plural_of

logger = logging.getLogger(__name__)

TYPE_KEY = "tagalot-type"
PATH_KEY = "path"
"""A notes-folder note's key naming its item by the end of its path (#658)."""


@dataclass(frozen=True)
class FolderNoteMeta:
    """What a notes-folder note is matched by."""

    title: str
    aliases: tuple[str, ...] = ()
    type: str | None = None
    path: str | None = None
    """``path``, cleaned (:func:`clean_path`): the item is the one whose folder or file
    path ends in it (#658)."""


def type_names(schema: ThemeSchema) -> dict[str, str]:
    """Each way of naming a type (its class name, label, and plural, and the end of its id
    and that end's plural, folded), to its id. The id's name lasts when a type is renamed
    (the movies theme's actors, shown as People since #449, still take ``Notes/Actors``)."""
    names: dict[str, str] = {}
    for entity, table in schema.entities.items():
        for name in (entity.__name__, entity_label(entity), entity_plural(entity)):
            names.setdefault(search_key(name), table.type_id)
        stored = table.type_id.rpartition(".")[2]
        names.setdefault(search_key(stored), table.type_id)
        names.setdefault(search_key(plural_of(stored)), table.type_id)
    return names


def note_type(
    names: Mapping[str, str], value: object, folder: str
) -> tuple[str | None, str | None]:
    """The type a note is for, from its ``tagalot-type`` ``value`` (if any), else the outermost
    of its ``folder``'s folders (inside the notes folder; ``""``: the notes folder itself)
    that names a type (``People/Family``: People, #656); with a problem to report when the
    key names no type."""
    problem = None
    if value is not None:
        text = as_text(value)
        if text and (found := names.get(search_key(text))) is not None:
            return found, None
        problem = f"{TYPE_KEY}: {text!r} is no type of this keep"
    return folder_type(names, folder), problem


def folder_type(names: Mapping[str, str], folder: str) -> str | None:
    """The type the outermost of ``folder``'s folders names (``"People/Family"`` and
    ``"People/Movies"``: People; ``""`` or none naming one: ``None``, any type)."""
    for part in folder.split("/"):
        if part and (found := names.get(search_key(part))) is not None:
            return found
    return None


def clean_path(value: object) -> str | None:
    """A note's ``path`` as matched: ``/`` between folders (``\\`` too, as Windows writes
    them), no empty or ``.`` parts; a leading ``/`` (from the top of the watched folder)
    is kept. ``None`` for none."""
    text = as_text(value) if not isinstance(value, list | tuple) else None
    if not text:
        return None
    text = text.replace("\\", "/")
    parts = [p.strip() for p in text.split("/")]
    parts = [p for p in parts if p and p != "."]
    if not parts:
        return None
    return ("/" if text.startswith("/") else "") + "/".join(parts)


def path_matches(path: str, relpath: str) -> bool:
    """Whether an item's root-relative ``relpath`` ends in a note's cleaned ``path``, by whole
    folder and file names, case aside (``Holidays/Alice`` matches ``Home Movies/Holidays/
    Alice``, not ``Home Movies/Alice`` or ``Old Holidays/Alice``); a leading ``/`` matches
    from the top of the watched folder."""
    anchored = path.startswith("/")
    wanted = path.strip("/").casefold().split("/")
    parts = relpath.casefold().split("/")
    if anchored:
        return parts == wanted
    return len(parts) >= len(wanted) and parts[len(parts) - len(wanted) :] == wanted


def _inside(relpath: str, notes_path: str) -> str:
    """The folder a note at ``relpath`` is in, inside the notes folder at ``notes_path``."""
    folder = posixpath.dirname(relpath)
    return folder[len(notes_path) :].strip("/") if notes_path else folder


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
    type_id, problem = note_type(names, front.get(TYPE_KEY), _inside(relpath, notes_path))
    meta = FolderNoteMeta(
        title,
        tuple(a for x in aliases if (a := as_text(x)) is not None),
        type_id,
        clean_path(front.get(PATH_KEY)),
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
    renames: set[int] = field(default_factory=set)
    """Items whose note names them with its ``title`` (#662): it has a path or aliases."""
    take: list[int] = field(default_factory=list)
    """Items whose notes-folder note goes."""
    warnings: list[IngestWarning] = field(default_factory=list)


def plan_folder_notes(
    reader: Engine, schema: ThemeSchema, root_id: str | None, notes_path: str, path: str
) -> FolderNotesPlan:
    """Read the notes folder's notes that changed and match every note to an item (in the
    caller's thread: a worker's). ``path`` is this computer's path for the root. With no
    notes folder (``root_id`` ``None``), or notes left outside a moved one, items let the
    notes they had from it go."""
    plan = FolderNotesPlan(root_id or "")
    with reader.connect() as conn:
        files = (
            []
            if root_id is None
            else [
                (NoteFile(rid, rel, size, mtime), status)
                for rid, rel, size, mtime, status in conn.execute(
                    select(
                        Resource.id,
                        Resource.relpath,
                        Resource.size,
                        Resource.mtime_ns,
                        Resource.status,
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
        )
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

    def stored_meta(row: Any, relpath: str) -> FolderNoteMeta:
        # No type stored: no tagalot-type named one, so its folders give it, as they are
        # now (a note stored before #656 finds a type folder above its own, unread).
        type_id = row.type or folder_type(names, _inside(relpath, notes_path))
        return FolderNoteMeta(row.title, tuple(row.aliases), type_id, row.path)

    for file, status in files:
        if status == ResourceStatus.MISSING:
            continue  # gone: its item lets it go
        row = stored.get(file.resource_id)
        if row is not None and (
            status != ResourceStatus.OK or (row.size, row.mtime_ns) == (file.size, file.mtime_ns)
        ):
            metas[file.resource_id] = stored_meta(row, file.relpath)
            continue
        if status != ResourceStatus.OK:
            continue
        text = read(file)
        if text is None:
            if row is not None:
                metas[file.resource_id] = stored_meta(row, file.relpath)
            continue
        texts[file.resource_id] = text
        metas[file.resource_id], problems = note_meta(text, file.relpath, notes_path, names)
        plan.warnings += [IngestWarning(root_id, file.relpath, p) for p in problems]

    # Matching: titles and aliases against items' titles, ignoring case and accents.
    index: dict[str, list[tuple[int, str]]] = {}
    for entity_id, type_id, title in items:
        index.setdefault(search_key(title), []).append((entity_id, type_id))
    ours = set(stored) | set(by_id)  # notes from the notes folder, as it is or was
    folder_notes = {e for e, (rid, _, _) in notes.items() if rid not in ours}
    current = {e: rid for e, (rid, _, _) in notes.items() if rid in ours}
    places = _item_paths(reader, schema) if any(m.path for m in metas.values()) else []
    matched: dict[int, list[int]] = {}
    for rid, meta in metas.items():
        if meta.path is not None:  # the item whose path ends in it, title aside (#658)
            hits = {
                entity_id
                for entity_id, type_id, relpath in places
                if (meta.type is None or type_id == meta.type) and path_matches(meta.path, relpath)
            }
        else:
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
            meta = metas[rid]
            if meta.path is not None or meta.aliases:  # it finds the item otherwise
                plan.renames.add(item)
    plan.take = [item for item, rid in current.items() if item not in owner]
    return plan


def _item_paths(reader: Engine, schema: ThemeSchema) -> list[tuple[int, str, str]]:
    """Each item's folder or files (those in its type's primary role) as (item, type, the
    root-relative path), for notes that give a ``path`` (#658)."""
    primary = {
        table.type_id: role.name
        for entity, table in schema.entities.items()
        for role in entity.roles
        if role.primary
    }
    with reader.connect() as conn:
        rows = conn.execute(
            select(EntityResource.entity_id, Entity.type, EntityResource.role, Resource.relpath)
            .join(Entity, Entity.id == EntityResource.entity_id)
            .join(Resource, Resource.id == EntityResource.resource_id)
            .where(Resource.status != ResourceStatus.MISSING)
        ).all()
    return [(e, t, rel) for e, t, role, rel in rows if primary.get(t) == role]


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
            "path": row.meta.path,
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
    aliases = applied_aliases(conn)
    own = applied_own_fields(conn)
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
            infos = schema.by_type_id(type_id).fields
            # path names the item (#658): never a field or an Extra field.
            values = map_note(text, infos, {*ignored, PATH_KEY}, aliases, own.get(type_id, ()))
            warnings += [
                IngestWarning(plan.root_id, file.relpath, f"A note's value was left out: {p}")
                for p in values.problems
            ]
        folder = folders.get(item)
        if apply_note(
            ctx,
            entity,
            values,
            file,
            folder_id=folder,
            folder_note=False,
            root_id=plan.root_id,
            renames=item in plan.renames,
        ):
            reread += naming_files(conn, schema, entity)
    ctx.flush()
    return reread, warnings + ctx.warnings


def notes_folder_files(conn: Connection) -> set[int]:
    """The notes folder's notes (their resources)."""
    return set(conn.scalars(select(NotesFolderNote.resource_id)))
