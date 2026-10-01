"""Linking files to items by hand (DESIGN.md §5, §9, §12; #243).

A link the user makes is marked ``by_user``. Themes don't see it from the file's side
(``ctx.entities_of``), so re-reading the file never renames or regroups the item; they can't
unlink it, and their links don't displace it in a one-file role. Otherwise it is the item's
file like any other: thumbnails, opening, the detail page.

:func:`link_files` and :func:`unlink_file` are each one undo step (an
:class:`~tagalot.core.actions.ActionChange` over the item's snapshot, which records links).
"""

from collections.abc import Sequence

from sqlalchemy import Connection, delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core.actions import ActionChange
from tagalot.core.entity_state import ChangeRecorder
from tagalot.core.models import Entity, EntityResource, Resource, ResourceKind
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import KIND_EXTENSIONS, Kind, Role
from tagalot.themes.api import Entity as ThemeEntity


class LinkError(ValueError):
    """A link that can't be made; the message is for the user."""


def kind_of_file(kind: ResourceKind, ext: str) -> Kind | None:
    """What kind of resource this is (as roles name them)."""
    if kind is ResourceKind.DIR:
        return Kind.DIR
    return next((k for k, exts in KIND_EXTENSIONS.items() if ext.lower() in exts), None)


def accepting_roles(entity: type[ThemeEntity], kinds: Sequence[Kind | None]) -> list[Role]:
    """The entity type's roles that accept every one of these kinds of file."""
    return [r for r in entity.roles if all(Kind.ANY in r.kinds or k in r.kinds for k in kinds)]


def file_kinds(conn: Connection, resource_ids: Sequence[int]) -> list[Kind | None]:
    return [
        kind_of_file(kind, ext)
        for kind, ext in conn.execute(
            select(Resource.kind, Resource.ext).where(Resource.id.in_(resource_ids))
        )
    ]


def link_files(
    conn: Connection,
    schema: ThemeSchema,
    entity_id: int,
    resource_ids: Sequence[int],
    role: str,
) -> ActionChange:
    """Link files to an item in ``role`` by hand. In a one-file role the file replaces
    whatever is there."""
    entity, title = _entity(conn, schema, entity_id)
    declared = next((r for r in entity.roles if r.name == role), None)
    if declared is None:
        raise LinkError(f"{title} has no {role!r} role.")
    ids = list(dict.fromkeys(resource_ids))
    if not ids:
        raise LinkError("Choose a file to link.")
    if not declared.many and len(ids) > 1:
        raise LinkError(f"{title}'s {role} holds one file; choose just one.")
    kinds = file_kinds(conn, ids)
    if len(kinds) != len(ids):
        raise LinkError("A file to link no longer exists.")
    if declared not in accepting_roles(entity, kinds):
        raise LinkError(f"{title}'s {role} doesn't take that kind of file.")

    recorder = ChangeRecorder(conn, schema)
    recorder.touch([entity_id])
    if not declared.many:
        conn.execute(
            delete(EntityResource).where(
                EntityResource.entity_id == entity_id, EntityResource.role == role
            )
        )
    last = conn.scalar(
        select(func.max(EntityResource.sort_order)).where(
            EntityResource.entity_id == entity_id, EntityResource.role == role
        )
    )
    start = 0 if last is None else last + 1
    for n, resource_id in enumerate(ids):
        conn.execute(
            sqlite_insert(EntityResource)
            .values(
                entity_id=entity_id,
                resource_id=resource_id,
                role=role,
                sort_order=start + n,
                by_user=True,
            )
            .on_conflict_do_update(
                index_elements=["entity_id", "resource_id", "role"], set_={"by_user": True}
            )
        )
    _forget_thumbnail(conn, entity_id)
    files = "1 file" if len(ids) == 1 else f"{len(ids)} files"
    return ActionChange(f"Link {files} to {title}", recorder.before, recorder.after())


def unlink_file(
    conn: Connection, schema: ThemeSchema, entity_id: int, resource_id: int, role: str
) -> ActionChange:
    """Remove a link the user made (theme links aren't the user's to remove)."""
    _, title = _entity(conn, schema, entity_id)
    by_user = conn.scalar(
        select(EntityResource.by_user).where(
            EntityResource.entity_id == entity_id,
            EntityResource.resource_id == resource_id,
            EntityResource.role == role,
        )
    )
    if not by_user:
        raise LinkError("Only links you made can be removed here.")
    recorder = ChangeRecorder(conn, schema)
    recorder.touch([entity_id])
    conn.execute(
        delete(EntityResource).where(
            EntityResource.entity_id == entity_id,
            EntityResource.resource_id == resource_id,
            EntityResource.role == role,
        )
    )
    _forget_thumbnail(conn, entity_id)
    return ActionChange(f"Unlink a file from {title}", recorder.before, recorder.after())


def _entity(conn: Connection, schema: ThemeSchema, entity_id: int) -> tuple[type[ThemeEntity], str]:
    row = conn.execute(select(Entity.type, Entity.title).where(Entity.id == entity_id)).first()
    if row is None:
        raise LinkError("That item no longer exists.")
    try:
        return schema.by_type_id(row.type).entity, row.title
    except KeyError:
        raise LinkError(f"{row.title} isn't one of this theme's items.") from None


def _forget_thumbnail(conn: Connection, entity_id: int) -> None:
    """Its files changed: its thumbnail source is chosen afresh (DESIGN.md §10)."""
    conn.execute(update(Entity).where(Entity.id == entity_id).values(thumb_resource_id=None))
