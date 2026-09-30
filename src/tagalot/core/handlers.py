"""Opening files, revealing them in the file manager, and "Open with…" (DESIGN.md §11).

This module decides *what* to open and *which command* does it; the UI launches it (the OS
default through ``QDesktopServices``, commands as detached processes). Opening only reads:
nothing here writes under a root (AGENTS.md rule 1).

- :func:`files_to_open` lists an entity's files: its primary role's, else all it links.
- :func:`choose_file` picks the first one that can be opened, or explains why none can.
- :func:`reveal_command` and :func:`open_with_command` give the command for a platform.
"""

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, select

from tagalot.core.detail import FileRow, role_files
from tagalot.core.models import Entity, ResourceStatus
from tagalot.core.theme_schema import ThemeSchema

OPEN, REVEAL, OPEN_WITH = "open", "reveal", "open_with"
HOWS = (OPEN, REVEAL, OPEN_WITH)
"""The ways to open a file: the OS default, in the file manager, or with a chosen program."""


class CannotOpen(Exception):
    """Nothing can be opened; the message says why, for the status bar."""


@dataclass(frozen=True)
class FileToOpen:
    resource_id: int
    path: str
    """This machine's path."""
    is_dir: bool


@dataclass(frozen=True)
class Command:
    """A program to start, detached, with its arguments."""

    program: str
    args: tuple[str, ...]


def files_to_open(
    conn: Connection,
    schema: ThemeSchema,
    entity_id: int,
    root_path: Callable[[str], str | None],
) -> tuple[FileRow, ...]:
    """The entity's files, best first: those of its type's primary role (in their sort
    order), else every file it links."""
    type_id = conn.scalar(select(Entity.type).where(Entity.id == entity_id))
    primary = None
    if type_id is not None:
        try:
            entity = schema.by_type_id(type_id).entity
        except KeyError:
            entity = None
        if entity is not None:
            primary = next((r.name for r in entity.roles if r.primary), None)
    if primary is not None:
        files = role_files(conn, entity_id, primary, root_path)
        if files:
            return files
    return role_files(conn, entity_id, None, root_path)


def resource_to_open(
    conn: Connection, resource_id: int, root_path: Callable[[str], str | None]
) -> tuple[FileRow, ...]:
    """One resource, as :func:`choose_file` takes it (empty if it's gone)."""
    return role_files(conn, 0, None, root_path, resource_id=resource_id)


def choose_file(files: Sequence[FileRow]) -> FileToOpen:
    """The first file that can be opened: known to be there at the last scan, with a path on
    this computer. Otherwise :class:`CannotOpen` says why the first one can't."""
    for file in files:
        if file.status is ResourceStatus.OK and file.path is not None:
            return FileToOpen(file.resource_id, file.path, file.kind == "dir")
    if not files:
        raise CannotOpen("Nothing to open: this item has no files.")
    first = files[0]
    name = first.relpath.rpartition("/")[2] or first.root_name
    if first.path is None:
        raise CannotOpen(f"Can't open {name}: {first.root_name} has no path on this computer.")
    if first.status is ResourceStatus.OFFLINE:
        raise CannotOpen(f"Can't open {name}: {first.root_name} was offline at the last scan.")
    raise CannotOpen(f"Can't open {name}: it was missing at the last scan.")


def reveal_command(file: FileToOpen, platform: str = sys.platform) -> Command | None:
    """The command that shows the file selected in the file manager, or ``None`` where
    there is none (Linux): then open its folder instead (:func:`reveal_folder`)."""
    if platform == "win32":
        # Explorer takes "/select," and the path as separate words (quoted if needed).
        return Command("explorer.exe", ("/select,", file.path))
    if platform == "darwin":
        return Command("open", ("-R", file.path))
    return None


def reveal_folder(file: FileToOpen, platform: str = sys.platform) -> str:
    """The folder to open when the file manager can't select a file: the file's folder (a
    folder is opened itself)."""
    if file.is_dir:
        return file.path
    separator = "\\" if platform == "win32" else "/"
    folder = file.path.rstrip(separator).rpartition(separator)[0]
    return folder or file.path


def open_with_command(file: FileToOpen, program: str, platform: str = sys.platform) -> Command:
    """The command that opens the file with a program the user chose (once, not
    remembered): an application bundle on macOS, an executable elsewhere."""
    if platform == "darwin":
        return Command("open", ("-a", program, file.path))
    return Command(program, (file.path,))
