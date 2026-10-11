"""Opening files, revealing them in the file manager, and "Open with…" (DESIGN.md §11).

This module decides *what* to open and *which command* does it; the UI launches it (the OS
default through ``QDesktopServices``, commands as detached processes). Opening only reads:
nothing here writes under a root (AGENTS.md rule 1).

- :func:`files_to_open` lists an entity's files: its primary role's, else all it links.
- :func:`choose_file` picks the first one that can be opened, or explains why none can.
- :func:`reveal_command` and :func:`open_with_command` give the command for a platform.
- Per-user overrides (``[[handlers]]`` in ``settings.toml``) are command templates:
  :func:`expand_command` turns one into a command for a file, and :func:`program_command`
  writes one for a program the user picked.
"""

import ntpath
import posixpath
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, select

from tagalot.core.detail import FileRow, note_file, role_files
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
    role: str | None = None
    """The role the item links it in (per-role overrides use it)."""

    @property
    def ext(self) -> str:
        """The extension, lowercased with its dot (``".psd"``), or ``""``."""
        return posixpath.splitext(_name(self.path))[1].lower()


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
    conn: Connection, entity_id: int, resource_id: int, root_path: Callable[[str], str | None]
) -> tuple[FileRow, ...]:
    """One of the entity's files, or its note's (#380), as :func:`choose_file` takes it
    (empty if it's gone)."""
    found = role_files(conn, entity_id, None, root_path, resource_id=resource_id)
    if not found:
        note = note_file(conn, entity_id, root_path)
        if note is not None and note.resource_id == resource_id:
            return (note,)
    return found


def choose_file(files: Sequence[FileRow]) -> FileToOpen:
    """The first file that can be opened: known to be there at the last scan, with a path on
    this computer. Otherwise :class:`CannotOpen` says why the first one can't."""
    for file in files:
        if file.status is ResourceStatus.OK and file.path is not None:
            return FileToOpen(file.resource_id, file.path, file.kind == "dir", file.role)
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


# --- per-user overrides (DESIGN.md §11) ---


def split_command(template: str) -> list[str]:
    """Split a command template into words: spaces separate them, double quotes group them
    (and are removed). Backslashes are ordinary, so Windows paths need no escaping:
    ``"C:\\Program Files\\Krita\\krita.exe" "{path}"``."""
    words: list[str] = []
    current: list[str] = []
    quoted = in_word = False
    for c in template:
        if c == '"':
            quoted = not quoted
            in_word = True
        elif c.isspace() and not quoted:
            if in_word:
                words.append("".join(current))
                current, in_word = [], False
        else:
            current.append(c)
            in_word = True
    if in_word:
        words.append("".join(current))
    return words


def expand_command(template: str, file: FileToOpen) -> Command:
    """The command a template gives for ``file``: ``{path}``, ``{dir}`` (its folder), and
    ``{name}`` (its file name) are filled in each word, so a path with spaces stays one
    argument. A template without placeholders gets the path as its last argument."""
    words = split_command(template)
    if not words:
        raise CannotOpen("The command for this kind of file is empty.")
    values = {"path": file.path, "dir": _folder(file.path), "name": _name(file.path)}
    if not any("{" in w for w in words):
        words.append("{path}")
    expanded = [w.format(**values) for w in words]
    return Command(expanded[0], tuple(expanded[1:]))


def program_command(program: str, platform: str = sys.platform) -> str:
    """The template that opens files with ``program``, a program the user picked (an
    application bundle on macOS)."""
    if platform == "darwin" and program.rstrip("/").endswith(".app"):
        return f'open -a "{program}" "{{path}}"'
    return f'"{program}" "{{path}}"'


def program_name(template: str) -> str:
    """A short name for the program a template runs, for menus: ``Krita``."""
    words = split_command(template)
    if words[:2] == ["open", "-a"] and len(words) > 2:
        words = words[2:]
    if not words:
        return "?"
    name = _name(words[0].rstrip("/\\"))
    stem, ext = posixpath.splitext(name)
    return stem if ext.lower() in (".exe", ".app", ".bat", ".cmd") and stem else name


def _name(path: str) -> str:
    return ntpath.basename(path) if "\\" in path else posixpath.basename(path)


def _folder(path: str) -> str:
    return ntpath.dirname(path) if "\\" in path else posixpath.dirname(path)
