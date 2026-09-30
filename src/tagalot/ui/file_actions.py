"""Opening an item's file, revealing it in the file manager, and "Open with…" (DESIGN.md §11).

Pages add the actions with :func:`add_file_actions` and emit a request; the window's
:class:`FileOpener` finds the file in a worker (:mod:`tagalot.core.handlers`) and then
launches it: the OS default through ``QDesktopServices`` (which handles UNC and mapped
paths), other commands as detached processes, so nothing waits on the program.
"""

import ctypes
import logging
import sys
from collections.abc import Callable

from PySide6.QtCore import QObject, QProcess, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMenu, QWidget

from tagalot.core.detail import FileRow
from tagalot.core.handlers import (
    OPEN,
    OPEN_WITH,
    REVEAL,
    CannotOpen,
    Command,
    FileToOpen,
    choose_file,
    files_to_open,
    open_with_command,
    resource_to_open,
    reveal_command,
    reveal_folder,
)
from tagalot.core.session import KeepSession
from tagalot.core.theme_schema import ThemeSchema
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

PLATFORM: str = sys.platform
"""A plain string, so type checkers don't drop the other platforms' branches."""


def file_kind(schema: ThemeSchema, type_id: str) -> str | None:
    """``"folder"`` when the type's primary role holds folders (an album), ``"file"`` when
    it can link files, ``None`` when it has no roles (nothing to open)."""
    try:
        entity = schema.by_type_id(type_id).entity
    except KeyError:
        return None
    if not entity.roles:
        return None
    primary = next((r for r in entity.roles if r.primary), None)
    if primary is not None and primary.kinds is not None and set(primary.kinds) == {"dir"}:
        return "folder"
    return "file"


def add_file_actions(menu: QMenu, request: Callable[[str], None], *, folder: bool = False) -> None:
    """Add "Open file", "Show in file manager", and "Open with…" to ``menu``; each calls
    ``request`` with its way of opening (``"open"``, ``"reveal"``, ``"open_with"``).
    ``folder`` names them for an item whose file is a folder (an album)."""
    open_action = menu.addAction("Open folder" if folder else "Open file")
    open_action.setToolTip("Open it with this computer's default program")
    open_action.triggered.connect(lambda: request(OPEN))
    reveal = menu.addAction("Show in file manager")
    reveal.triggered.connect(lambda: request(REVEAL))
    if not folder:
        open_with = menu.addAction("Open with…")
        open_with.setToolTip("Choose a program to open it with, this once")
        open_with.triggered.connect(lambda: request(OPEN_WITH))


class FileOpener(QObject):
    """Opens items' files for one window; ``message`` explains what couldn't be done."""

    message = Signal(str)

    def __init__(self, session: KeepSession, pool: QThreadPool | None, window: QWidget) -> None:
        super().__init__(window)
        self.session = session
        self._pool = pool
        self._window = window

    def open_entity(self, entity_id: int, how: str) -> None:
        """Open the entity's first file that can be opened (its primary role's)."""
        session = self.session

        def job() -> tuple[FileRow, ...]:
            with session.reader.connect() as conn:
                return files_to_open(conn, session.schema, entity_id, self._root_path)

        run_in_pool(job, on_done=lambda files: self._chosen(files, how), pool=self._pool)

    def open_resource(self, resource_id: int, how: str) -> None:
        """Open one of an item's files (a row on its page)."""
        session = self.session

        def job() -> tuple[FileRow, ...]:
            with session.reader.connect() as conn:
                return resource_to_open(conn, resource_id, self._root_path)

        run_in_pool(job, on_done=lambda files: self._chosen(files, how), pool=self._pool)

    def _root_path(self, root_id: str) -> str | None:
        try:
            return self.session.root_path(root_id)
        except StopIteration:  # a root no longer in keep.toml
            return None

    def _chosen(self, files: tuple[FileRow, ...], how: str) -> None:
        try:
            file = choose_file(files)
        except CannotOpen as e:
            self.message.emit(str(e))
            return
        if how == REVEAL:
            self.reveal(file)
        elif how == OPEN_WITH:
            self.open_with(file)
        else:
            self.open(file)

    # --- launching (tests replace these three) ---

    def open(self, file: FileToOpen) -> None:
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(file.path)):
            self.message.emit(f"No program opened {file.path}.")

    def reveal(self, file: FileToOpen) -> None:
        command = reveal_command(file)
        if command is None:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(reveal_folder(file))):
                self.message.emit(f"Couldn't show {file.path} in the file manager.")
            return
        self.start(command)

    def open_with(self, file: FileToOpen) -> None:
        if PLATFORM == "win32":
            if not _windows_open_with(file.path, int(self._window.winId())):
                self.message.emit(f"Couldn't show the Open with dialog for {file.path}.")
            return
        if PLATFORM == "darwin":
            program = QFileDialog.getOpenFileName(
                self._window, "Open with", "/Applications", "Applications (*.app)"
            )[0]
        else:
            program = QFileDialog.getOpenFileName(self._window, "Open with a program", "/usr/bin")[
                0
            ]
        if program:
            self.start(open_with_command(file, program))

    def start(self, command: Command) -> None:
        """Start a program without waiting for it."""
        started, _ = QProcess.startDetached(command.program, list(command.args))
        if not started:
            logger.warning("Couldn't start %s %s", command.program, command.args)
            self.message.emit(f"Couldn't start {command.program}.")


def _windows_open_with(path: str, hwnd: int) -> bool:
    """Windows' own "Open with" dialog, for this one file (the choice isn't remembered)."""

    class OpenAsInfo(ctypes.Structure):
        _fields_ = [
            ("pcszFile", ctypes.c_wchar_p),
            ("pcszClass", ctypes.c_wchar_p),
            ("oaifInFlags", ctypes.c_int),
        ]

    oaif_exec, oaif_hide_registration = 0x04, 0x20
    info = OpenAsInfo(path, None, oaif_exec | oaif_hide_registration)
    try:
        shell32 = getattr(ctypes, "windll").shell32  # noqa: B009 (Windows only)
        result = shell32.SHOpenWithDialog(hwnd, ctypes.byref(info))
    except (AttributeError, OSError) as e:
        logger.warning("Open with failed for %s: %s", path, e)
        return False
    # S_OK, or the user cancelling (HRESULT_FROM_WIN32(ERROR_CANCELLED)).
    return result in (0, -2147023673)
