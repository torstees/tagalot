"""**Save as note…**: an item's note typed in Tagalot, written as a new note file in the
keep's notes folder (DESIGN.md §9 *In-app notes and Save as note…*, #383).

This is the third code path that writes under a root (AGENTS.md rule 1), and it is narrow
on purpose:

- only into the keep's notes folder, only when ``[notes] writable`` (marked on its own, so
  a notes folder inside a content folder is written without making that folder writable),
  and only when the user asks;
- only a new file: an existing file of that name is reported, never replaced (the file is
  created exclusively), and the only folder made is the type's subfolder in the notes
  folder;
- file names are made safe for every system (:func:`safe_name`).

The note has front matter with ``title``, ``tagalot-type`` (in the one-folder layout, or
when the type's folder name could mean another type), and the item's tags as full paths
(``Genre/Fantasy``, as Write to file… writes them), then the typed body. The next scan
reads it, and the item's note is the file's from then on.
"""

import logging
import os
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Connection, select

from tagalot.core.keep import KeepConfig
from tagalot.core.models import Entity, EntityNote, EntityTag
from tagalot.core.notes import NOTE_EXTENSIONS
from tagalot.core.notes_folder import TYPE_KEY, type_names
from tagalot.core.roots import local_path
from tagalot.core.tags import TagTree, search_key
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writeback import edit_front_matter, tag_changes
from tagalot.themes.api import entity_label, entity_plural

logger = logging.getLogger(__name__)

UNSAFE = re.compile(r'[:?*/\\"<>|\x00-\x1f\x7f]')
"""Characters some system forbids in a file name; each becomes :data:`REPLACEMENT`."""
REPLACEMENT = "_"
RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)
"""Names Windows keeps for devices, whatever the extension."""
MAX_NAME = 120
"""Characters of a title kept in a file name (well under every system's limit)."""
UNTITLED = "Untitled"


class SaveNoteError(ValueError):
    """The note can't be written; the message is meant for the user."""


def safe_name(title: str) -> str:
    """``title`` as a file name every system takes: forbidden characters replaced, spaces
    collapsed, no leading or trailing dot or space, not a device name, not too long."""
    name = " ".join(UNSAFE.sub(REPLACEMENT, title).split())[:MAX_NAME].strip(" .")
    if not name:
        return UNTITLED
    if name.split(".")[0].rstrip().casefold() in RESERVED:
        name = REPLACEMENT + name
    return name


@dataclass(frozen=True)
class NewNote:
    """A note file to write: where (root-relative and on this computer) and its text."""

    entity_id: int
    title: str
    root_id: str
    relpath: str
    path: str
    notes_folder: str
    """The notes folder on this computer: it must be there; only a subfolder is made."""
    text: str


def plan_note(
    conn: Connection,
    schema: ThemeSchema,
    tree: TagTree,
    config: KeepConfig,
    root_path: Callable[[str], str],
    entity_id: int,
) -> NewNote:
    """Work out the note file for an item (reading only the keep). Raises
    :class:`SaveNoteError` when there is no writable notes folder, the item is gone, or its
    note is a file already."""
    if config.notes_root is None:
        raise SaveNoteError("The keep has no notes folder (Keep configuration → Notes).")
    if not config.notes_writable:
        raise SaveNoteError(
            "Tagalot may not write in the notes folder (Keep configuration → Notes)."
        )
    row = conn.execute(select(Entity.title, Entity.type).where(Entity.id == entity_id)).first()
    if row is None:
        raise SaveNoteError("This item no longer exists.")
    note = conn.execute(
        select(EntityNote.resource_id, EntityNote.body).where(EntityNote.entity_id == entity_id)
    ).first()
    if note is not None and note.resource_id is not None:
        raise SaveNoteError(f"The note of {row.title!r} is a file already.")
    entity = schema.by_type_id(row.type).entity
    plural = entity_plural(entity)
    folder = safe_name(plural)
    typed = config.new_notes == "one-folder" or (
        type_names(schema).get(search_key(folder)) != row.type
    )
    relfolder = config.notes_path
    if config.new_notes != "one-folder":
        relfolder = posixpath.join(relfolder, folder) if relfolder else folder
    name = safe_name(row.title) + NOTE_EXTENSIONS[0]
    relpath = posixpath.join(relfolder, name) if relfolder else name
    front: dict[str, object] = {"title": row.title}
    if typed:
        front[TYPE_KEY] = entity_label(entity)
    item_tags = conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id == entity_id))
    front.update(tag_changes({}, item_tags, (), tree))
    body = (note.body if note is not None else None) or ""
    text = edit_front_matter(f"\n{body}\n" if body else "", front)
    base = root_path(config.notes_root)
    path = local_path(base, relpath)
    notes_folder = local_path(base, config.notes_path) if config.notes_path else base
    return NewNote(entity_id, row.title, config.notes_root, relpath, path, notes_folder, text)


def write_note(note: NewNote) -> None:
    """Write the note (call it from a worker): a new file only, never replacing one. Raises
    :class:`SaveNoteError` when the file is there already or can't be written."""
    if not os.path.isdir(note.notes_folder):
        raise SaveNoteError(f"The notes folder can't be reached: {note.notes_folder}")
    folder = os.path.dirname(note.path)
    try:
        if not os.path.isdir(folder):
            os.mkdir(folder)  # the type's subfolder, in the notes folder
        with open(note.path, "x", encoding="utf-8", newline="\n") as file:
            file.write(note.text)
    except FileExistsError:
        raise SaveNoteError(
            f"{note.relpath} is there already; nothing was written. Rename or move it, "
            "or open it and copy the note in."
        ) from None
    except OSError as e:
        logger.warning("Couldn't write the note %s: %s", note.path, e)
        raise SaveNoteError(f"{note.relpath} couldn't be written: {e.strerror or e}") from e
    logger.info("Saved the note of %r as %s", note.title, note.path)
