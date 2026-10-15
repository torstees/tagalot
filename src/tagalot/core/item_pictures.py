"""Items' own pictures (DESIGN.md §10 *Thumbnails*, #439), for every type of every theme.

The thumbnail resolver tries an item's own picture (``item_picture``) before its theme's
chain. It comes from:

1. **The user** (``user``): **Set picture…** on the item's page, or **Use as its picture**
   on a gallery picture; **Use the usual picture** goes back. One undo step each
   (:class:`PictureChange`).
2. **Its note** (``note``), worked out after each scan by :func:`fill_note_pictures`: the
   picture the note names (``cover``, else ``image``; relative to the note's folder, or to
   the top of its watched folder with a leading ``/``), else, for a note in the notes
   folder, a picture beside it named like it (``Terry Pratchett.jpg`` beside
   ``Terry Pratchett.md``). A named picture that isn't there is listed in Triage →
   Pictures (``file_not_found``, role :data:`PICTURE_ROLE`).

An item can have both: the user's is used, and forgetting it shows the note's at once.
After the theme's chain the resolver tries the item's first gallery picture, when the chain
found no picture.
"""

import posixpath
import re
from dataclasses import dataclass

from sqlalchemy import Connection, delete, func, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core.models import (
    Entity,
    EntityNote,
    FileNotFound,
    ItemPicture,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from tagalot.core.scanner import in_notes
from tagalot.themes.api import KIND_EXTENSIONS, Kind
from tagalot.themes.readers import no_path

USER, NOTE = "user", "note"
PICTURE_ROLE = "picture"
"""What a note's missing picture is listed as in Triage → Pictures (not a theme's role)."""
PICTURES = KIND_EXTENSIONS[Kind.IMAGE] | KIND_EXTENSIONS[Kind.ARCHIVE]
"""What can be an item's picture: a picture, or an archive (its best picture)."""


def is_picture(relpath: str) -> bool:
    """Whether a file can be an item's picture, by its extension."""
    return posixpath.splitext(relpath)[1].lower() in PICTURES


# --- the note's ---


def picture_path(note_relpath: str, written: str) -> tuple[str | None, str | None]:
    """Where a picture a note names is in its watched folder, or why it can't be looked
    for: relative to the note's folder, or to the top with a leading ``/``; Obsidian's
    ``[[cover.jpg]]`` too."""
    value = written.strip().removeprefix("![[").removeprefix("[[").removesuffix("]]")
    value = value.replace("\\", "/")
    if re.match(r"^[a-z]:(/|$)", value, re.IGNORECASE):
        return None, "It's a path on a drive, not inside the watched folder."
    if re.match(r"^[a-z][a-z0-9+.-]*:", value, re.IGNORECASE):
        return None, "It's a web address: Tagalot only shows pictures in your folders."
    folder = "" if value.startswith("/") else posixpath.dirname(note_relpath)
    path = posixpath.normpath(posixpath.join(folder, value.lstrip("/")))
    if path in (".", "..") or path.startswith("../"):
        return None, "It isn't a path inside the watched folder."
    return path, None


def _usable(root_id: str) -> list[object]:
    return [
        Resource.root_id == root_id,
        Resource.kind == ResourceKind.FILE,
        Resource.status != ResourceStatus.MISSING,
        Resource.skipped.is_(False),
        Resource.parent_resource_id.is_(None),
    ]


def _find(conn: Connection, root_id: str, relpath: str) -> int | None:
    """The picture at ``relpath`` (case aside when no file matches exactly)."""
    query = select(Resource.id, Resource.ext).where(*_usable(root_id))  # type: ignore[arg-type]
    row = conn.execute(query.where(Resource.relpath == relpath)).first()
    if row is None:
        lower = query.where(func.lower(Resource.relpath) == relpath.lower())
        row = conn.execute(lower.order_by(Resource.relpath).limit(1)).first()
    return row.id if row is not None and row.ext.lower() in PICTURES else None


def _beside(conn: Connection, root_id: str, note_relpath: str) -> int | None:
    """A picture beside a note, named like it (``Terry Pratchett.jpg``)."""
    folder, _, name = note_relpath.rpartition("/")
    stem = posixpath.splitext(name)[0].casefold()
    query = select(Resource.id, Resource.relpath, Resource.ext).where(*_usable(root_id))  # type: ignore[arg-type]
    if folder:
        query = query.where(Resource.relpath > folder + "/", Resource.relpath < folder + "0")
    for rid, relpath, ext in conn.execute(query.order_by(Resource.relpath)):
        inside = relpath[len(folder) + 1 :] if folder else relpath
        if "/" in inside or ext.lower() not in KIND_EXTENSIONS[Kind.IMAGE]:
            continue
        if posixpath.splitext(inside)[0].casefold() == stem:
            return int(rid)
    return None


def fill_note_pictures(conn: Connection, root_id: str, notes_folder: str | None = None) -> int:
    """Bring the pictures the root's notes give their items up to date; how many items'
    pictures changed. ``notes_folder`` is the keep's notes folder when it is in this root:
    a note there gives a picture beside it, too. Runs in the writer, after notes are
    applied."""
    notes = conn.execute(
        select(EntityNote.entity_id, EntityNote.picture, Resource.id, Resource.relpath)
        .join(Resource, Resource.id == EntityNote.resource_id)
        .where(Resource.root_id == root_id, Resource.status != ResourceStatus.MISSING)
    ).all()
    wanted: dict[int, int] = {}
    missing: list[dict[str, object]] = []
    for entity_id, written, note_id, note_relpath in notes:
        if written and not no_path(written):
            path, reason = picture_path(note_relpath, written)
            found = _find(conn, root_id, path) if path is not None else None
            if found is not None:
                wanted[entity_id] = found
            else:
                missing.append(
                    {
                        "entity_id": entity_id,
                        "resource_id": note_id,
                        "role": PICTURE_ROLE,
                        "written": written,
                        "tried": [path] if path else [],
                        "reason": reason,
                    }
                )
        elif in_notes(note_relpath, notes_folder):
            found = _beside(conn, root_id, note_relpath)
            if found is not None:
                wanted[entity_id] = found
    note_ids = [n for _, _, n, _ in notes]
    if note_ids:
        conn.execute(
            delete(FileNotFound).where(
                FileNotFound.resource_id.in_(note_ids), FileNotFound.role == PICTURE_ROLE
            )
        )
    if missing:
        conn.execute(insert(FileNotFound), missing)
    current = dict(
        conn.execute(
            select(ItemPicture.entity_id, ItemPicture.resource_id)
            .join(Resource, Resource.id == ItemPicture.resource_id)
            .where(ItemPicture.source == NOTE, Resource.root_id == root_id)
        ).all()
    )
    changed: set[int] = set()
    for entity_id in set(current) - set(wanted):
        conn.execute(
            delete(ItemPicture).where(
                ItemPicture.entity_id == entity_id, ItemPicture.source == NOTE
            )
        )
        changed.add(entity_id)
    for entity_id, resource_id in wanted.items():
        if current.get(entity_id) == resource_id:
            continue
        upsert = sqlite_insert(ItemPicture).values(
            entity_id=entity_id, resource_id=resource_id, source=NOTE
        )
        conn.execute(
            upsert.on_conflict_do_update(
                index_elements=["entity_id", "source"], set_={"resource_id": resource_id}
            )
        )
        changed.add(entity_id)
    _forget_thumbnails(conn, changed)
    return len(changed)


def _forget_thumbnails(conn: Connection, ids: set[int]) -> None:
    if ids:
        conn.execute(update(Entity).where(Entity.id.in_(ids)).values(thumb_resource_id=None))


# --- the user's ---


@dataclass(frozen=True)
class OwnPicture:
    """Where an item's own picture comes from, for its page."""

    resource_id: int
    relpath: str
    source: str
    """``user`` or ``note``."""


def own_picture(conn: Connection, entity_id: int) -> OwnPicture | None:
    """The item's own picture: the user's, else its note's."""
    row = conn.execute(
        select(ItemPicture.resource_id, Resource.relpath, ItemPicture.source)
        .join(Resource, Resource.id == ItemPicture.resource_id)
        .where(ItemPicture.entity_id == entity_id)
        .order_by(ItemPicture.source != USER)
        .limit(1)
    ).first()
    return None if row is None else OwnPicture(*row)


@dataclass(frozen=True)
class PictureChange:
    """An item's own picture set or cleared by the user; one undo step."""

    label: str
    entity_id: int
    before: int | None
    """The picture the user had chosen, if any."""
    after: int | None


class PictureError(ValueError):
    """The file can't be an item's picture."""


def set_picture(
    conn: Connection, entity_id: int, resource_id: int | None, label: str
) -> PictureChange:
    """Make ``resource_id`` the item's picture (``None``: use the usual picture again,
    forgetting the user's choice, so its note's, else its theme's, is shown)."""
    before = conn.scalar(
        select(ItemPicture.resource_id).where(
            ItemPicture.entity_id == entity_id, ItemPicture.source == USER
        )
    )
    if resource_id is not None:
        ext = conn.scalar(select(Resource.ext).where(Resource.id == resource_id))
        if ext is None or ext.lower() not in PICTURES:
            raise PictureError("Choose a picture (or an archive of pictures) as its picture.")
    change = PictureChange(label, entity_id, before, resource_id)
    restore_picture(conn, change, forward=True)
    return change


def restore_picture(conn: Connection, change: PictureChange, *, forward: bool) -> None:
    """Apply (``forward``) or undo a :class:`PictureChange`."""
    picture = change.after if forward else change.before
    conn.execute(
        delete(ItemPicture).where(
            ItemPicture.entity_id == change.entity_id, ItemPicture.source == USER
        )
    )
    if picture is not None:
        still = conn.scalar(select(Resource.id).where(Resource.id == picture))
        if still is not None:
            conn.execute(
                insert(ItemPicture).values(
                    entity_id=change.entity_id, resource_id=picture, source=USER
                )
            )
    _forget_thumbnails(conn, {change.entity_id})
