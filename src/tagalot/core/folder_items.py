"""Folder items (#447, DESIGN.md §9 *Folder items*): folders of pictures as items that hold
what's in them.

A theme names its folder type with ``folders_for`` (``Folder.folders_for = (Picture,
Archive)``). After a root's files are ingested, :func:`fill_folders` makes a folder item for
every folder above a file of those types (the folder it's in, and each folder above it, up
to the top of the root, which is never one), except a folder that is already another item's
primary folder (a collection's); it keeps one the user tagged, and deletes the rest that are
no longer wanted. Each folder item holds the items the theme lets it contain whose primary
files (or, for an item that is a folder, the folder) are nearest under it. The containment
goes through :class:`IngestSession`, so what the user put in or took out by hand stays, and
closure and the search index are kept in step.

A folder item's fields that mirror a containment (``people: str | None = field("People",
mirrors=Person, many=True)``, with ``contains(Person, Folder)``) are what its note says it
belongs to: :func:`link_folder_fields`, after the notes are read, finds or makes each item
named (:meth:`Theme.item_named`) and puts the folder in it, and takes it out of those of that
type no longer named (#449: Alice's folder note names her).
"""

import posixpath
from collections.abc import Iterable

from sqlalchemy import Connection, select

from tagalot.core.ingest import IngestSession
from tagalot.core.linked_fields import names_in
from tagalot.core.models import (
    Entity,
    EntityContains,
    EntityResource,
    EntityTag,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from tagalot.core.theme_schema import EntityTable, ThemeSchema
from tagalot.themes.api import EntityRef, Role

FOLDER_KEY = "folder:{root}:{path}"
"""A folder item's ingest key: its root and its path in it."""


def folder_table(schema: ThemeSchema) -> EntityTable | None:
    """The theme's folder items type (``folders_for``), if it has one."""
    return next((t for t in schema.entities.values() if t.entity.folders_for), None)


def _primary(table: EntityTable) -> Role | None:
    return next((r for r in table.entity.roles if r.primary), None)


def _above(path: str) -> Iterable[str]:
    """``path`` and each folder above it, nearest first, without the root's top (``""``)."""
    while path:
        yield path
        path = posixpath.dirname(path)


def fill_folders(conn: Connection, schema: ThemeSchema, root_id: str) -> int:
    """Bring the root's folder items and what they hold up to date; how many items or
    edges changed. Runs in the writer, after the root's files are ingested."""
    folders = folder_table(schema)
    if folders is None:
        return 0
    folder_role = _primary(folders)
    assert folder_role is not None  # the loader checks it
    theme = schema.theme
    tables = {t.type_id: t for t in schema.entities.values()}
    primaries = {type_id: r.name for type_id, t in tables.items() if (r := _primary(t))}
    makers = {theme.type_id_of(e) for e in folders.entity.folders_for}
    held = {theme.type_id_of(c.child) for c in theme.containment if c.parent is folders.entity}
    declared = {(theme.type_id_of(c.parent), theme.type_id_of(c.child)) for c in theme.containment}

    # Every item with a primary file or folder in this root: where its files are.
    rows = conn.execute(
        select(Entity.id, Entity.type, EntityResource.role, Resource.relpath, Resource.kind)
        .join(EntityResource, EntityResource.entity_id == Entity.id)
        .join(Resource, Resource.id == EntityResource.resource_id)
        .where(Resource.root_id == root_id, Resource.status != ResourceStatus.MISSING)
    ).all()
    places: dict[int, tuple[str, list[str]]] = {}
    """Item -> its type, and where to look for what holds it: the folders its primary files
    are in, or, for an item that is a folder, the folder above it."""
    owned: dict[str, str] = {}
    """Folders that are another item's primary folder (a collection's), with its type: never
    folder items."""
    existing: dict[str, int] = {}
    """The root's folder items, by path."""
    for entity_id, type_id, role, relpath, kind in rows:
        if primaries.get(type_id) != role:
            continue
        if kind is ResourceKind.DIR:
            if type_id == folders.type_id:
                existing[relpath] = entity_id
            else:
                owned[relpath] = type_id
        # A file: the folder it's in; a folder: the one above it.
        places.setdefault(entity_id, (type_id, []))[1].append(posixpath.dirname(relpath))

    wanted: set[str] = set()
    for type_id, paths in places.values():
        if type_id in makers:
            wanted.update(p for path in paths for p in _above(path) if p not in owned)

    tagged = set(
        conn.scalars(
            select(EntityTag.entity_id)
            .where(EntityTag.entity_id.in_(existing.values()), EntityTag.by_file.is_(False))
            .distinct()
        )
    )
    wanted |= {path for path, entity_id in existing.items() if entity_id in tagged}

    dirs = dict(
        conn.execute(
            select(Resource.relpath, Resource.id).where(
                Resource.root_id == root_id,
                Resource.kind == ResourceKind.DIR,
                Resource.status == ResourceStatus.OK,
                Resource.relpath.in_(wanted),
            )
        ).all()
    )
    wanted &= set(dirs)

    ctx = IngestSession(conn, schema)
    changed = 0
    refs: dict[str, EntityRef] = {}
    for path in sorted(wanted):
        if path in existing:
            refs[path] = EntityRef(existing[path], folders.type_id)
            continue
        key = FOLDER_KEY.format(root=root_id, path=path)
        refs[path] = ctx.upsert(folders.entity, key, title=posixpath.basename(path))
        ctx.link(refs[path], dirs[path], folder_role.name)
        changed += 1
    for path, entity_id in existing.items():
        if path not in wanted:
            ctx.delete(EntityRef(entity_id, folders.type_id))
            changed += 1

    def holder(start: str, type_id: str) -> str | None:
        """The folder item nearest above ``start`` (itself included) that holds an item of
        ``type_id``: none past a collection's folder that holds such items itself."""
        for path in _above(start):
            if path in refs:
                return path
            owner = owned.get(path)
            if owner is not None and (owner, type_id) in declared:
                return None
        return None

    # What each folder item should hold: the items nearest under it.
    should: set[tuple[int, int]] = set()
    for path, ref in refs.items():
        nearest = holder(posixpath.dirname(path), folders.type_id)
        if nearest is not None and folders.type_id in held:
            should.add((refs[nearest].id, ref.id))
    for entity_id, (type_id, paths) in places.items():
        if type_id == folders.type_id or type_id not in held:
            continue
        for path in paths:
            nearest = holder(path, type_id)
            if nearest is not None:
                should.add((refs[nearest].id, entity_id))
    folder_ids = {ref.id for ref in refs.values()}
    holds: set[tuple[int, int]] = {
        (parent, child)
        for parent, child in conn.execute(
            select(EntityContains.parent_id, EntityContains.child_id).where(
                EntityContains.parent_id.in_(folder_ids)
            )
        )
    }
    types = {entity_id: type_id for entity_id, (type_id, _) in places.items()}
    types.update({ref.id: folders.type_id for ref in refs.values()})
    for parent, child in sorted(should - holds):
        ctx.contain(EntityRef(parent, folders.type_id), EntityRef(child, types[child]))
        changed += 1
    for parent, child in sorted(holds - should):
        child_type = types.get(child) or conn.scalar(select(Entity.type).where(Entity.id == child))
        if child_type is None or child_type not in held:
            continue
        ctx.uncontain(EntityRef(parent, folders.type_id), EntityRef(child, child_type))
        changed += 1
    ctx.flush()
    return changed


def link_folder_fields(conn: Connection, schema: ThemeSchema, root_id: str) -> int:
    """Put each of the root's folder items in the items its mirrored fields name (as its
    note gave them, or the user typed), and take it out of those of that type no longer
    named; how many links changed. Runs in the writer, after the root's notes are read.
    What the user put in or took out by hand stays."""
    folders = folder_table(schema)
    if folders is None:
        return 0
    theme = schema.theme
    declared = {(c.parent, c.child) for c in theme.containment}
    mirrored = [
        f
        for f in folders.fields
        if isinstance(f.spec.mirrors, type) and (f.spec.mirrors, folders.entity) in declared
    ]
    if not mirrored:
        return 0
    role = _primary(folders)
    assert role is not None
    ids = list(
        conn.scalars(
            select(EntityResource.entity_id)
            .join(Resource, Resource.id == EntityResource.resource_id)
            .join(Entity, Entity.id == EntityResource.entity_id)
            .where(
                Entity.type == folders.type_id,
                EntityResource.role == role.name,
                Resource.root_id == root_id,
            )
        )
    )
    if not ids:
        return 0
    table = folders.table
    rows = conn.execute(
        select(table.c.id, *(table.c[f.name] for f in mirrored)).where(table.c.id.in_(ids))
    ).all()
    ctx = IngestSession(conn, schema)
    instance = theme()
    changed = 0
    for row in rows:
        folder = EntityRef(row[0], folders.type_id)
        for position, info in enumerate(mirrored, start=1):
            mirror = info.spec.mirrors
            assert isinstance(mirror, type)
            mirror_id = theme.type_id_of(mirror)
            wanted = {instance.item_named(mirror, n, ctx).id for n in names_in(row[position])}
            current = set(
                conn.scalars(
                    select(EntityContains.parent_id)
                    .join(Entity, Entity.id == EntityContains.parent_id)
                    .where(EntityContains.child_id == folder.id, Entity.type == mirror_id)
                )
            )
            for parent in sorted(wanted - current):
                ctx.contain(EntityRef(parent, mirror_id), folder)
                changed += 1
            for parent in sorted(current - wanted):
                ctx.uncontain(EntityRef(parent, mirror_id), folder)
                changed += 1
    ctx.flush()
    return changed
