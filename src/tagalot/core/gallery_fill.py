"""Gallery roles filled by the core (DESIGN.md §9 *Galleries*): from named folders (#387)
and from the notes folder (#388).

**Named folders.** A theme declares ``role("screenshots", kinds={"image", "archive"},
many=True, gallery=True, folders=("Screenshots", "Screens"))``. After a root's files are
ingested, :func:`fill_galleries` links to each item of the type, in that role, every
picture (and, when the role takes archives, every archive) under those subfolders of the
item's folder, at any depth, and the archives of those names beside them
(``Screenshots.zip``). Names are compared ignoring case. The item's folder is its primary
role's folder, else the folder of its primary file; a folder several items of the type
share (loose movies side by side) fills none of them.

**The item's own folder** (#448). ``role(..., gallery=True, own_folder=True)`` is filled
with every picture (and archive) under the item's folder, at any depth, when no other item
of its type has a primary file in it or below it, and never from the top of a root: a
story's folder with its renders. Files the item has in its other roles (its cover) aren't
added, nor a folder's image (``folder.jpg``, #458), nor another item's own file (a comic
archive beside a book), unless that item is a picture or archive of its own (a type the
theme's folder items are made for, #447).

**The notes folder.** A folder or archive named like a notes-folder note, beside it
(``Notes/Actors/Keanu Reeves/``, ``Keanu Reeves.zip``), fills the first gallery role of the
item the note is matched to (:func:`fill_note_galleries`, after the notes are matched),
with the pictures and archives inside at any depth. It follows the note: a note that stops
matching takes its gallery with it.

Links made here are marked ``by_folder``: each fill adds what is new and removes those
whose file is gone or no longer belongs. The two fills keep apart by where the file is:
files in the notes folder are the notes fill's, the rest the named folders'. The theme's
own links in the role, and the user's, are never removed (a filled link the user also made
stays theirs), and a file linked in the role by either isn't linked again. Files of an
offline root aren't looked at: no scan runs.
"""

import fnmatch
import posixpath
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import Connection, delete, insert, select, update

from tagalot.core.models import (
    Entity,
    EntityResource,
    NotesFolderNote,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from tagalot.core.scanner import in_notes
from tagalot.core.theme_schema import EntityTable, ThemeSchema
from tagalot.themes.api import FOLDER_IMAGE_NAMES, KIND_EXTENSIONS, Kind, Role

IMAGES = KIND_EXTENSIONS[Kind.IMAGE]
ARCHIVES = KIND_EXTENSIONS[Kind.ARCHIVE]
GALLERY_EXTENSIONS = IMAGES | ARCHIVES
"""What a gallery can hold; the notes folder records these besides its notes (#388)."""

Link = tuple[int, int, str]
"""(entity id, resource id, role)."""


@dataclass(frozen=True)
class _Gallery:
    table: EntityTable
    role: Role
    primary: Role | None
    names: frozenset[str]
    """The folder names, case-folded."""
    extensions: frozenset[str]
    """What the role takes: pictures, and archives when it has that kind."""
    own: bool = False
    """Filled from everything in the item's own folder (``own_folder``, #448)."""
    primaries: Mapping[str, str] = field(default_factory=dict)
    """Each type id's primary role (for ``own``)."""
    pictures: frozenset[str] = frozenset()
    """Type ids of pictures and archives of their own (``folders_for``), for ``own``."""


def _takes(role: Role) -> frozenset[str]:
    return IMAGES | (ARCHIVES if Kind.ARCHIVE in role.kinds else frozenset())


def gallery_roles(schema: ThemeSchema) -> list[_Gallery]:
    """The theme's gallery roles the core fills from folders (named ones, or the item's
    own), on types with a primary role."""
    theme = schema.theme
    primaries = {
        t.type_id: r.name for t in schema.entities.values() for r in t.entity.roles if r.primary
    }
    pictures = frozenset(
        theme.type_id_of(e) for t in schema.entities.values() for e in t.entity.folders_for
    )
    found = []
    for table in schema.entities.values():
        primary = next((r for r in table.entity.roles if r.primary), None)
        if primary is None:
            continue
        for role in table.entity.roles:
            if role.gallery and (role.folders or role.own_folder):
                names = frozenset(f.strip().casefold() for f in role.folders)
                found.append(
                    _Gallery(
                        table,
                        role,
                        primary,
                        names,
                        _takes(role),
                        role.own_folder,
                        primaries,
                        pictures,
                    )
                )
    return found


def first_galleries(schema: ThemeSchema) -> dict[str, _Gallery]:
    """Each type's first gallery role, by type id: what a notes-folder note's pictures fill."""
    found = {}
    for table in schema.entities.values():
        role = next((r for r in table.entity.roles if r.gallery), None)
        if role is not None:
            found[table.type_id] = _Gallery(table, role, None, frozenset(), _takes(role))
    return found


# --- named folders ---


def fill_galleries(
    conn: Connection, schema: ThemeSchema, root_id: str, notes_folder: str | None = None
) -> int:
    """Bring the root's items' folder-filled gallery links up to date; how many links were
    added or removed. ``notes_folder`` is the keep's notes folder when it is in this root:
    its files are left to :func:`fill_note_galleries`. Runs in the writer, after the root's
    files are ingested."""
    changed = 0
    for gallery in gallery_roles(schema):
        changed += _fill(conn, gallery, root_id, notes_folder)
    return changed


def _folders(conn: Connection, gallery: _Gallery, root_id: str) -> dict[int, str]:
    """Each item of the type with its primary file in this root: its folder, when no other
    item of the type has the same one."""
    assert gallery.primary is not None
    rows = conn.execute(
        select(EntityResource.entity_id, Resource.relpath, Resource.kind)
        .join(Resource, Resource.id == EntityResource.resource_id)
        .join(Entity, Entity.id == EntityResource.entity_id)
        .where(
            Entity.type == gallery.table.type_id,
            EntityResource.role == gallery.primary.name,
            Resource.root_id == root_id,
            Resource.status != ResourceStatus.MISSING,
        )
        .order_by(EntityResource.entity_id, EntityResource.sort_order, Resource.relpath)
    )
    folders: dict[int, str] = {}
    others: dict[int, list[str]] = {}
    for entity_id, relpath, kind in rows:
        if entity_id not in folders:
            folders[entity_id] = relpath if kind is ResourceKind.DIR else posixpath.dirname(relpath)
        elif gallery.own:  # its other files count for no one else's own folder
            others.setdefault(entity_id, []).append(posixpath.dirname(relpath))
    if gallery.own:
        return _own_folders(folders, others)
    # A folder several items share (loose movies side by side) is no one's: its named
    # subfolders can't be told apart (#389).
    shared = Counter(f.casefold() for f in folders.values())
    return {e: f for e, f in folders.items() if shared[f.casefold()] == 1}


def _own_folders(folders: dict[int, str], others: dict[int, list[str]]) -> dict[int, str]:
    """Each item's folder, when it isn't the top of the root and no other item has a
    primary file in it or below it (#448)."""
    owner: dict[str, int | None] = {}
    """Each folder (case-folded) with files of one item in it or below it, that item's id;
    ``None`` for a folder with several items'."""
    for entity_id, paths in [(e, [f]) for e, f in folders.items()] + list(others.items()):
        for path in paths:
            path = path.casefold()
            while path:
                if owner.get(path, entity_id) != entity_id:
                    owner[path] = None
                elif path not in owner:
                    owner[path] = entity_id
                path = posixpath.dirname(path)
    return {e: f for e, f in folders.items() if f and owner.get(f.casefold()) == e}


def _files(conn: Connection, root_id: str, folder: str) -> Iterable[tuple[int, str, str]]:
    """The usable files under ``folder`` (``""``: the whole root): id, path, extension."""
    query = select(Resource.id, Resource.relpath, Resource.ext).where(
        Resource.root_id == root_id,
        Resource.kind == ResourceKind.FILE,
        Resource.status != ResourceStatus.MISSING,
        Resource.skipped.is_(False),
        Resource.parent_resource_id.is_(None),
    )
    if folder:  # a range on the path, so the index finds them
        query = query.where(Resource.relpath > folder + "/", Resource.relpath < folder + "0")
    return [(rid, relpath, ext) for rid, relpath, ext in conn.execute(query)]


def _wanted(gallery: _Gallery, inside: str, ext: str) -> bool:
    """A file at ``inside`` (relative to the item's folder) belongs in the gallery."""
    ext = ext.lower()
    if ext not in gallery.extensions:
        return False
    if gallery.own:  # anything in its own folder, at any depth, but a folder's look
        return not folder_image(inside)
    first, _, rest = inside.partition("/")
    if rest:  # under a named subfolder, at any depth
        return first.casefold() in gallery.names
    stem = posixpath.splitext(first)[0]
    return ext in ARCHIVES and stem.casefold() in gallery.names  # an archive beside them


def folder_image(relpath: str) -> bool:
    """A picture named as a folder's own (``folder.jpg``: :data:`FOLDER_IMAGE_NAMES`), which
    is its folder's look rather than one of an own folder's pictures (#458)."""
    stem = posixpath.splitext(relpath.rpartition("/")[2])[0].lower()
    return any(fnmatch.fnmatchcase(stem, pattern) for pattern in FOLDER_IMAGE_NAMES)


def _others_files(conn: Connection, gallery: _Gallery, root_id: str) -> set[int]:
    """Files in this root that are an item's own (primary) file, other than pictures and
    archives of their own: not for an own folder's gallery (#448)."""
    rows = conn.execute(
        select(EntityResource.resource_id, Entity.type, EntityResource.role)
        .join(Resource, Resource.id == EntityResource.resource_id)
        .join(Entity, Entity.id == EntityResource.entity_id)
        .where(Resource.root_id == root_id, Entity.type.not_in(gallery.pictures))
    )
    return {rid for rid, type_id, role in rows if gallery.primaries.get(type_id) == role}


def _fill(conn: Connection, gallery: _Gallery, root_id: str, notes: str | None) -> int:
    folders = _folders(conn, gallery, root_id)
    role = gallery.role.name
    # Files the items have in their other roles (a cover) aren't added to an own folder's.
    elsewhere: set[tuple[int, int]] = (
        {
            (entity_id, rid)
            for entity_id, rid in conn.execute(
                select(EntityResource.entity_id, EntityResource.resource_id).where(
                    EntityResource.entity_id.in_(list(folders)), EntityResource.role != role
                )
            )
        }
        if gallery.own
        else set()
    )
    others = _others_files(conn, gallery, root_id) if gallery.own else set()
    wanted: set[Link] = set()
    for entity_id, folder in folders.items():
        for rid, relpath, ext in _files(conn, root_id, folder):
            inside = relpath[len(folder) + 1 :] if folder else relpath
            if (entity_id, rid) in elsewhere or rid in others:
                continue
            if not in_notes(relpath, notes) and _wanted(gallery, inside, ext):
                wanted.add((entity_id, rid, role))
    linked = conn.execute(
        select(
            EntityResource.entity_id,
            EntityResource.resource_id,
            EntityResource.by_folder,
            Resource.relpath,
        )
        .join(Resource, Resource.id == EntityResource.resource_id)
        .join(Entity, Entity.id == EntityResource.entity_id)
        .where(
            # Every item of the type, so one whose folder stopped being its own (or
            # became shared) loses what was filled.
            Entity.type == gallery.table.type_id,
            EntityResource.role == role,
            Resource.root_id == root_id,
        )
    ).all()
    present = {(e, r, role) for e, r, _, _ in linked}
    filled = {(e, r, role) for e, r, mine, rel in linked if mine and not in_notes(rel, notes)}
    return _sync(conn, wanted, present, filled)


# --- the notes folder ---


def fill_note_galleries(
    conn: Connection, schema: ThemeSchema, root_id: str | None, notes_path: str
) -> int:
    """Give each item matched to a notes-folder note the pictures and archives in the
    folder and archives named like the note, beside it, in its first gallery role; take
    them from items no longer matched. How many links were added or removed. Runs in the
    writer, after the notes folder's notes are matched. Without a notes folder there is
    nothing to do: once it's gone, scans of its root take its links back."""
    galleries = first_galleries(schema)
    if root_id is None or not galleries:
        return 0
    files: dict[tuple[str, str], list[tuple[int, str]]] = {}
    """(folder, name), case-folded -> the files under that folder's subfolder ``name`` (at
    any depth), and archives named ``name`` in it: (id, extension)."""
    for rid, relpath, ext in _files(conn, root_id, notes_path):
        ext = ext.lower()
        if ext not in GALLERY_EXTENSIONS:
            continue
        parts = relpath.split("/")
        for depth in range(len(parts) - 1):  # each folder above it
            key = ("/".join(parts[:depth]).casefold(), parts[depth].casefold())
            files.setdefault(key, []).append((rid, ext))
        if ext in ARCHIVES:  # an archive beside the note
            stem = posixpath.splitext(parts[-1])[0]
            key = ("/".join(parts[:-1]).casefold(), stem.casefold())
            files.setdefault(key, []).append((rid, ext))
    notes = conn.execute(
        select(NotesFolderNote.item_id, Resource.relpath, Entity.type)
        .join(Resource, Resource.id == NotesFolderNote.resource_id)
        .join(Entity, Entity.id == NotesFolderNote.item_id)
        .where(
            Resource.root_id == root_id,
            Resource.status != ResourceStatus.MISSING,
            NotesFolderNote.applied.is_(True),
        )
    ).all()
    wanted: set[Link] = set()
    for item_id, relpath, type_id in notes:
        gallery = galleries.get(type_id)
        if gallery is None or item_id is None:
            continue
        folder, _, name = relpath.rpartition("/")
        key = (folder.casefold(), posixpath.splitext(name)[0].casefold())
        for rid, ext in files.get(key, []):
            if ext in gallery.extensions:
                wanted.add((item_id, rid, gallery.role.name))
    roles = {g.role.name for g in galleries.values()}
    linked = conn.execute(
        select(
            EntityResource.entity_id,
            EntityResource.resource_id,
            EntityResource.role,
            EntityResource.by_folder,
            Resource.relpath,
        )
        .join(Resource, Resource.id == EntityResource.resource_id)
        .where(EntityResource.role.in_(roles), Resource.root_id == root_id)
    ).all()
    present = {(e, r, role) for e, r, role, _, _ in linked}
    filled = {
        (e, r, role) for e, r, role, mine, rel in linked if mine and in_notes(rel, notes_path)
    }
    return _sync(conn, wanted, present, filled)


# --- both ---


def _sync(conn: Connection, wanted: set[Link], present: set[Link], filled: set[Link]) -> int:
    """Add the wanted links that aren't there, and take back the filled ones no longer
    wanted (one the user also made stays, theirs alone). How many changed."""
    added = sorted(wanted - present)
    removed = sorted(filled - wanted)
    if added:
        conn.execute(
            insert(EntityResource),
            [
                {"entity_id": e, "resource_id": r, "role": role, "sort_order": 0, "by_folder": True}
                for e, r, role in added
            ],
        )
    for e, r, role in removed:
        link = (
            EntityResource.entity_id == e,
            EntityResource.resource_id == r,
            EntityResource.role == role,
            EntityResource.by_folder.is_(True),
        )
        conn.execute(delete(EntityResource).where(*link, EntityResource.by_user.is_(False)))
        conn.execute(update(EntityResource).where(*link).values(by_folder=False))
    touched = {e for e, _, _ in added} | {e for e, _, _ in removed}
    if touched:  # their thumbnails are chosen afresh
        conn.execute(update(Entity).where(Entity.id.in_(touched)).values(thumb_resource_id=None))
    return len(added) + len(removed)
