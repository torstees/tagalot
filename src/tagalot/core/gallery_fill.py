"""Gallery roles filled from named folders (DESIGN.md §9 *Galleries*, #387).

A theme declares ``role("screenshots", kinds={"image", "archive"}, many=True,
gallery=True, folders=("Screenshots", "Screens"))``. After a root's files are ingested,
:func:`fill_galleries` links to each item of the type, in that role, every picture (and,
when the role takes archives, every archive) under those subfolders of the item's folder,
at any depth, and the archives of those names beside them (``Screenshots.zip``). Names
are compared ignoring case. The item's folder is its primary role's folder, else the folder
of its primary file.

Links made here are marked ``by_folder``: each fill adds what is new and removes those
whose file is gone (missing, skipped, or no longer under a named folder). The theme's own
links in the role, and the user's, are never touched, and a file linked in the role by
either isn't linked again. Files of an offline root aren't looked at: no scan runs.
"""

import posixpath
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Connection, delete, insert, select, update

from tagalot.core.models import Entity, EntityResource, Resource, ResourceKind, ResourceStatus
from tagalot.core.theme_schema import EntityTable, ThemeSchema
from tagalot.themes.api import KIND_EXTENSIONS, Kind, Role

IMAGES = KIND_EXTENSIONS[Kind.IMAGE]
ARCHIVES = KIND_EXTENSIONS[Kind.ARCHIVE]


@dataclass(frozen=True)
class _Gallery:
    table: EntityTable
    role: Role
    primary: Role
    names: frozenset[str]
    """The folder names, case-folded."""
    extensions: frozenset[str]
    """What the role takes: pictures, and archives when it has that kind."""


def gallery_roles(schema: ThemeSchema) -> list[_Gallery]:
    """The theme's gallery roles with folders, on types with a primary role."""
    found = []
    for table in schema.entities.values():
        primary = next((r for r in table.entity.roles if r.primary), None)
        if primary is None:
            continue
        for role in table.entity.roles:
            if role.gallery and role.folders:
                takes = IMAGES | (ARCHIVES if Kind.ARCHIVE in role.kinds else frozenset())
                names = frozenset(f.strip().casefold() for f in role.folders)
                found.append(_Gallery(table, role, primary, names, takes))
    return found


def fill_galleries(conn: Connection, schema: ThemeSchema, root_id: str) -> int:
    """Bring the root's items' folder-filled gallery links up to date; how many links were
    added or removed. Runs in the writer, after the root's files are ingested."""
    changed = 0
    for gallery in gallery_roles(schema):
        changed += _fill(conn, gallery, root_id)
    return changed


def _folders(conn: Connection, gallery: _Gallery, root_id: str) -> dict[int, str]:
    """Each item of the type with its primary file in this root: its folder."""
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
    return folders


def _under(conn: Connection, root_id: str, folder: str) -> Iterable[tuple[int, str, str]]:
    """The usable files under ``folder`` (``""``: the whole root), with their paths
    relative to it and their extensions."""
    query = select(Resource.id, Resource.relpath, Resource.ext).where(
        Resource.root_id == root_id,
        Resource.kind == ResourceKind.FILE,
        Resource.status != ResourceStatus.MISSING,
        Resource.skipped.is_(False),
        Resource.parent_resource_id.is_(None),
    )
    if folder:  # a range on the path, so the index finds them
        query = query.where(Resource.relpath > folder + "/", Resource.relpath < folder + "0")
    for rid, relpath, ext in conn.execute(query):
        yield rid, relpath[len(folder) + 1 :] if folder else relpath, ext


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


def _fill(conn: Connection, gallery: _Gallery, root_id: str) -> int:
    folders = _folders(conn, gallery, root_id)
    if not folders:
        return 0
    role = gallery.role.name
    wanted: set[tuple[int, int]] = set()
    for entity_id, folder in folders.items():
        for rid, inside, ext in _under(conn, root_id, folder):
            if _wanted(gallery, inside, ext):
                wanted.add((entity_id, rid))
    ids = list(folders)
    linked = conn.execute(
        select(EntityResource.entity_id, EntityResource.resource_id, EntityResource.by_folder)
        .join(Resource, Resource.id == EntityResource.resource_id)
        .where(
            EntityResource.entity_id.in_(ids),
            EntityResource.role == role,
            Resource.root_id == root_id,
        )
    ).all()
    present = {(e, r) for e, r, _ in linked}
    filled = {(e, r) for e, r, by_folder in linked if by_folder}
    added = sorted(wanted - present)
    removed = sorted(filled - wanted)
    if added:
        conn.execute(
            insert(EntityResource),
            [
                {"entity_id": e, "resource_id": r, "role": role, "sort_order": 0, "by_folder": True}
                for e, r in added
            ],
        )
    for e, r in removed:
        link = (
            EntityResource.entity_id == e,
            EntityResource.resource_id == r,
            EntityResource.role == role,
            EntityResource.by_folder.is_(True),
        )
        conn.execute(delete(EntityResource).where(*link, EntityResource.by_user.is_(False)))
        # One the user also linked by hand stays theirs (#243).
        conn.execute(update(EntityResource).where(*link).values(by_folder=False))
    touched = {e for e, _ in added} | {e for e, _ in removed}
    if touched:  # their thumbnails are chosen afresh
        conn.execute(update(Entity).where(Entity.id.in_(touched)).values(thumb_resource_id=None))
    return len(added) + len(removed)
