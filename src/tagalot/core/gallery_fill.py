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

import posixpath
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

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
from tagalot.themes.api import KIND_EXTENSIONS, Kind, Role

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


def _takes(role: Role) -> frozenset[str]:
    return IMAGES | (ARCHIVES if Kind.ARCHIVE in role.kinds else frozenset())


def gallery_roles(schema: ThemeSchema) -> list[_Gallery]:
    """The theme's gallery roles with folders, on types with a primary role."""
    found = []
    for table in schema.entities.values():
        primary = next((r for r in table.entity.roles if r.primary), None)
        if primary is None:
            continue
        for role in table.entity.roles:
            if role.gallery and role.folders:
                names = frozenset(f.strip().casefold() for f in role.folders)
                found.append(_Gallery(table, role, primary, names, _takes(role)))
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
    for entity_id, relpath, kind in rows:
        if entity_id not in folders:
            folders[entity_id] = relpath if kind is ResourceKind.DIR else posixpath.dirname(relpath)
    # A folder several items share (loose movies side by side) is no one's: its named
    # subfolders can't be told apart (#389).
    shared = Counter(f.casefold() for f in folders.values())
    return {e: f for e, f in folders.items() if shared[f.casefold()] == 1}


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
    first, _, rest = inside.partition("/")
    if rest:  # under a named subfolder, at any depth
        return first.casefold() in gallery.names
    stem = posixpath.splitext(first)[0]
    return ext in ARCHIVES and stem.casefold() in gallery.names  # an archive beside them


def _fill(conn: Connection, gallery: _Gallery, root_id: str, notes: str | None) -> int:
    folders = _folders(conn, gallery, root_id)
    if not folders:
        return 0
    role = gallery.role.name
    wanted: set[Link] = set()
    for entity_id, folder in folders.items():
        for rid, relpath, ext in _files(conn, root_id, folder):
            inside = relpath[len(folder) + 1 :] if folder else relpath
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
        .where(
            EntityResource.entity_id.in_(list(folders)),
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
