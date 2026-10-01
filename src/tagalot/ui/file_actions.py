"""Opening an item's file, revealing it in the file manager, and "Open with" (DESIGN.md §11).

The window's :class:`FileOpener` is given to its pages (``page.file_opener``); they add the
actions with :func:`add_file_actions`. The opener finds the file in a worker
(:mod:`tagalot.core.handlers`) and then launches it: the user's override for its extension
if there is one, else the OS default through ``QDesktopServices`` (which handles UNC and
mapped paths). Commands start as detached processes, so nothing waits on the program.

"Open with" is a submenu filled once the file is known: a program for this once, "Always
open .ext files with…" (a per-user override in ``settings.toml``), and, when one applies,
"Stop using <program> for .ext files".
"""

import ctypes
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
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
    expand_command,
    files_to_open,
    open_with_command,
    program_command,
    program_name,
    resource_to_open,
    reveal_command,
    reveal_folder,
)
from tagalot.core.session import KeepSession
from tagalot.core.settings import HandlerOverride, save_settings
from tagalot.core.theme_schema import ThemeSchema
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

PLATFORM: str = sys.platform
"""A plain string, so type checkers don't drop the other platforms' branches."""

Lookup = Callable[[Callable[[FileToOpen | CannotOpen], None]], None]
"""Finds the file in a worker and calls back with it (or why there is none)."""


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


def opens_file(schema: ThemeSchema, type_id: str) -> bool:
    """Whether double-click (and Enter) opens an item's file rather than its page: its type
    says ``double_click = "open_file"`` and can link files. Ctrl+Enter does the other."""
    try:
        entity = schema.by_type_id(type_id).entity
    except KeyError:
        return False
    return entity.double_click == "open_file" and file_kind(schema, type_id) is not None


ALTERNATE_KEYS = (QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter"))
"""Ctrl+Enter (main keyboard and keypad): the other of page and file (DESIGN.md §12)."""


def add_alternate_keys(view: QWidget, handler: Callable[[], None]) -> None:
    """Call ``handler`` on Ctrl+Enter in ``view`` (before the view sees it as Enter)."""
    for keys in ALTERNATE_KEYS:
        shortcut = QShortcut(keys, view, context=Qt.ShortcutContext.WidgetShortcut)
        shortcut.activated.connect(handler)


def add_file_actions(
    menu: QMenu,
    opener: "FileOpener",
    *,
    entity_id: int,
    resource_id: int | None = None,
    folder: bool = False,
) -> QAction:
    """Add "Open file" (or "Open folder"), "Show in file manager", and, for files, the "Open
    with" submenu, for the entity's file (or one of them, ``resource_id``). Returns the
    "Open file" action."""

    def lookup(on_done: Callable[[FileToOpen | CannotOpen], None]) -> None:
        opener.lookup(entity_id, resource_id, on_done)

    open_action = menu.addAction("Open folder" if folder else "Open file")
    open_action.setToolTip("Open it with its usual program")
    open_action.triggered.connect(lambda: lookup(lambda f: opener.act(f, OPEN)))
    reveal = menu.addAction("Show in file manager")
    reveal.triggered.connect(lambda: lookup(lambda f: opener.act(f, REVEAL)))
    if not folder:
        menu.addMenu(OpenWithMenu(opener, lookup, menu))
    return open_action


class OpenWithMenu(QMenu):
    """ "Open with": filled when first shown, once the file (and its extension) is known."""

    def __init__(self, opener: "FileOpener", lookup: Lookup, parent: QWidget | None) -> None:
        super().__init__("Open with", parent)
        self.opener = opener
        self._lookup = lookup
        self._filled = False
        self.addAction("Looking for the file\u2026").setEnabled(False)
        self.aboutToShow.connect(self.fill)

    def fill(self) -> None:
        """Look the file up (once) and list the choices for it."""
        if self._filled:
            return
        self._filled = True
        self._lookup(self._found)

    def _found(self, file: FileToOpen | CannotOpen) -> None:
        self.clear()
        if isinstance(file, CannotOpen):
            self.addAction(str(file)).setEnabled(False)
            return
        once = self.addAction("Choose a program\u2026")
        once.setToolTip("Open it with a program you choose, this once")
        once.triggered.connect(lambda: self.opener.act(file, OPEN_WITH))
        self.addSeparator()
        kind = f"{file.ext} files" if file.ext else "files without an extension"
        always = self.addAction(f"Always open {kind} with\u2026")
        always.setToolTip("Choose the program Open file uses for these, on this computer")
        always.triggered.connect(lambda: self.opener.choose_default(file))
        rule = self.opener.override_for(file)
        if rule is not None:
            name = program_name(rule.command)
            forget = self.addAction(f"Stop using {name} for {kind}")
            forget.setToolTip(f"Open file goes back to the usual program. The rule: {rule.command}")
            forget.triggered.connect(lambda: self.opener.forget_default(file))


class FileOpener(QObject):
    """Opens items' files for one window; ``message`` says what happened when it matters."""

    message = Signal(str)

    def __init__(self, session: KeepSession, pool: QThreadPool | None, window: QWidget) -> None:
        super().__init__(window)
        self.session = session
        self._pool = pool
        self._window = window
        self.settings_path: Path | None = None
        """Where overrides are saved (``None``: the user's ``settings.toml``)."""

    # --- finding the file ---

    def lookup(
        self,
        entity_id: int,
        resource_id: int | None,
        on_done: Callable[[FileToOpen | CannotOpen], None],
    ) -> None:
        """Find the entity's file (or its ``resource_id``) in a worker; call ``on_done``
        with it, or with why there's none."""
        session = self.session

        def job() -> FileToOpen | CannotOpen:
            with session.reader.connect() as conn:
                files: tuple[FileRow, ...] = (
                    files_to_open(conn, session.schema, entity_id, self._root_path)
                    if resource_id is None
                    else resource_to_open(conn, entity_id, resource_id, self._root_path)
                )
            try:
                return choose_file(files)
            except CannotOpen as e:
                return e

        run_in_pool(job, on_done=on_done, pool=self._pool)

    def open_entity(self, entity_id: int, how: str = OPEN) -> None:
        """Open the entity's first file that can be opened (its primary role's)."""
        self.lookup(entity_id, None, lambda file: self.act(file, how))

    def _root_path(self, root_id: str) -> str | None:
        try:
            return self.session.root_path(root_id)
        except StopIteration:  # a root no longer in keep.toml
            return None

    def act(self, file: FileToOpen | CannotOpen, how: str) -> None:
        """Open, reveal, or "open with" a found file; or say why there's none."""
        if isinstance(file, CannotOpen):
            self.message.emit(str(file))
        elif how == REVEAL:
            self.reveal(file)
        elif how == OPEN_WITH:
            self.open_with(file)
        elif (rule := self.override_for(file)) is not None:
            try:
                self.start(expand_command(rule.command, file))
            except (CannotOpen, KeyError, IndexError, ValueError) as e:
                self.message.emit(f"The command for {file.ext} files doesn't work: {e}")
        else:
            self.open(file)

    # --- overrides ---

    def override_for(self, file: FileToOpen) -> HandlerOverride | None:
        """The user's rule for this file's extension (and role), if any."""
        if file.is_dir or not file.ext:
            return None
        return self.session.settings.handler_for(file.ext, file.role)

    def choose_default(self, file: FileToOpen) -> None:
        """Ask for a program, then always open files like this one with it."""
        program = self.pick_program(f"Always open {file.ext or 'these'} files with")
        if not program:
            return
        rule = self.session.settings.set_handler(file.ext, program_command(program, PLATFORM))
        self._save()
        self.message.emit(
            f"Open file now uses {program_name(rule.command)} for {rule.ext} files. "
            "Open with \u2192 Stop using\u2026 undoes it."
        )

    def forget_default(self, file: FileToOpen) -> None:
        rule = self.override_for(file)
        if rule is None:
            return
        self.session.settings.remove_handler(rule)
        self._save()
        self.message.emit(f"Open file uses the usual program for {rule.ext} files again.")

    def _save(self) -> None:
        settings, path = self.session.settings, self.settings_path

        def failed(error: BaseException) -> None:
            self.message.emit(f"Couldn't save your settings: {error}")

        run_in_pool(lambda: save_settings(settings, path), on_error=failed, pool=self._pool)

    # --- launching (tests replace these) ---

    def pick_program(self, title: str) -> str:
        """Ask for a program (an application on macOS); ``""`` if cancelled."""
        if PLATFORM == "darwin":
            start, filter_ = "/Applications", "Applications (*.app)"
        elif PLATFORM == "win32":
            start, filter_ = "C:\\Program Files", "Programs (*.exe *.bat *.cmd)"
        else:
            start, filter_ = "/usr/bin", ""
        return QFileDialog.getOpenFileName(self._window, title, start, filter_)[0]

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
        """A program chosen this once: Windows' own dialog, else a program to pick."""
        if PLATFORM == "win32":
            if not _windows_open_with(file.path, int(self._window.winId())):
                self.message.emit(f"Couldn't show the Open with dialog for {file.path}.")
            return
        program = self.pick_program("Open with")
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
