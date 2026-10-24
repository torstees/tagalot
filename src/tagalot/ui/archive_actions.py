"""What to read inside archives, from right-click menus (#519, DESIGN.md §12 *Archives*).

The window's :class:`ArchiveActions` is given to its pages (``page.archive_actions``), which
add its commands to menus:

- on an archive file: **Choose what to read inside…** (the dialog), **Read everything
  inside**, and **Stop reading inside**;
- on a file inside an archive: **Hide inside its archive**;
- on a folder: **Read everything inside archives here** (a ``keep.toml`` pattern).

Choices are read and saved in workers; members no longer read are skipped at once (their
items hide, keeping their tags), and a scan of the archive's folder reads the ones now
wanted. The archive itself is only ever read.
"""

from collections.abc import Callable

import shiboken6
from PySide6.QtCore import QObject, QThreadPool, Signal
from PySide6.QtWidgets import QMenu, QWidget
from sqlalchemy import select
from sqlalchemy.orm import aliased

from tagalot.core.archive_choices import (
    ArchiveChoices,
    archive_choices,
    hide_members,
    read_everything,
    read_folder_archives,
    save_choices,
    stop_reading,
)
from tagalot.core.archive_members import ARCHIVE_EXTENSIONS
from tagalot.core.handlers import CannotOpen, FileToOpen
from tagalot.core.member_paths import member_name
from tagalot.core.models import ArchiveMode, Resource
from tagalot.core.roots import local_path
from tagalot.core.session import KeepSession
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import Kind
from tagalot.ui.archive_dialog import ChooseInsideDialog, left_out_message
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.workers import run_in_pool

CHOOSE = "Choose what to read inside…"
READ_ALL = "Read everything inside"
STOP = "Stop reading inside"
HIDE = "Hide inside its archive"
READ_FOLDER = "Read everything inside archives here"


class ArchiveActions(QObject):
    """The window's commands for what is read inside archives."""

    changed = Signal(str)
    """Choices were saved (what lists show may change); the text says what happened."""
    message = Signal(str)
    """Nothing changed, or something failed: the text says why."""
    scan_requested = Signal(list)
    """Scan these roots (by id), to read members now wanted."""
    config_saved = Signal()
    """``keep.toml`` changed (a folder's archives are read now)."""

    def __init__(self, session: KeepSession, pool: QThreadPool | None, window: QWidget) -> None:
        super().__init__(window)
        self.session = session
        self._pool = pool
        self._window = window
        self.dialog: ChooseInsideDialog | None = None
        """The dialog open now, if any."""

    # --- menus ---

    def add_archive_actions(self, menu: QMenu, archive_id: int) -> None:
        """The commands of an archive file (not one inside another)."""
        choose = menu.addAction(CHOOSE)
        choose.setToolTip("Pick the files inside it that Tagalot reads, and hide the others")
        choose.triggered.connect(lambda: self.choose(archive_id))
        everything = menu.addAction(READ_ALL)
        everything.setToolTip("Read every file inside it, and those added to it later")
        everything.triggered.connect(lambda: self.read_everything(archive_id))
        stop = menu.addAction(STOP)
        stop.setToolTip(
            "Read nothing inside it: the items of its files hide, keeping their tags for "
            "if it's read again"
        )
        stop.triggered.connect(lambda: self.stop_reading(archive_id))

    def add_hide(self, menu: QMenu, member_id: int) -> None:
        """Hide inside its archive, for a file inside one."""
        hide = menu.addAction(HIDE)
        hide.setToolTip(
            "Don't read this file inside its archive: its item hides, keeping its tags. "
            "The archive's Choose what to read inside… shows it again."
        )
        hide.triggered.connect(lambda: self.hide(member_id))

    def add_folder_action(self, menu: QMenu, root_id: str, folder: str) -> None:
        """Read everything inside archives here, for a folder (root-relative)."""
        here = menu.addAction(READ_FOLDER)
        here.setToolTip(
            "Read the files inside every archive in this folder and those under it, from "
            "now on (Keep configuration → Folders → Read inside archives)"
        )
        here.triggered.connect(lambda: self.read_folder(root_id, folder))

    # --- the commands ---

    def choose(self, archive_id: int) -> None:
        """Open the dialog for the archive (once its choices are read)."""
        self._with_choices(archive_id, self._open_dialog)

    def read_everything(self, archive_id: int) -> None:
        def act(current: ArchiveChoices) -> None:
            if current.mode is ArchiveMode.ALL and not current.hidden():
                self.message.emit(f"Everything inside {current.name} is read already.")
                return
            self._write(
                lambda conn: read_everything(conn, current),
                f"Everything inside {current.name} will be read: scanning to read it.",
                current.root_id,
            )

        self._with_choices(archive_id, act)

    def stop_reading(self, archive_id: int) -> None:
        def act(current: ArchiveChoices) -> None:
            if current.mode is ArchiveMode.NONE:
                self.message.emit(f"Nothing inside {current.name} is read already.")
                return
            self._write(
                lambda conn: stop_reading(conn, current),
                f"Tagalot no longer reads inside {current.name}. The items of its files are "
                "hidden; their tags are kept for if it's read again.",
            )

        self._with_choices(archive_id, act)

    def hide(self, member_id: int) -> None:
        """Hide one file inside its archive."""
        session = self.session

        def find() -> tuple[int, str] | None:
            archive = aliased(Resource)
            with session.reader.connect() as conn:
                row = conn.execute(
                    select(archive.id, archive.relpath, Resource.relpath)
                    .join(archive, archive.id == Resource.parent_resource_id)
                    .where(Resource.id == member_id)
                ).first()
            return None if row is None else (row[0], member_name(row[1], row[2]))

        def found(where: tuple[int, str] | None) -> None:
            if where is None:
                self.message.emit("That file isn't inside an archive any more.")
                return
            archive_id, name = where

            def act(current: ArchiveChoices) -> None:
                self._write(
                    lambda conn: hide_members(conn, current, [name]),
                    f"Hid {name.rpartition('/')[2]} inside {current.name}; its item's tags "
                    f"are kept. Right-click {current.name} → {CHOOSE} shows it again.",
                )

            self._with_choices(archive_id, act)

        run_in_pool(find, on_done=found, on_error=self._failed, pool=self._pool)

    def read_folder(self, root_id: str, folder: str) -> None:
        """Read the archives in a folder whole, with an ``archives`` pattern."""
        session = self.session

        def job() -> str:
            config, pattern = read_folder_archives(
                session.keep.config, session.keep.dir, root_id, folder
            )
            if config is not session.keep.config:
                session.save_config(config)
            return pattern

        def done(pattern: str) -> None:
            if not shiboken6.isValid(self):
                return
            self.config_saved.emit()
            where = folder.rpartition("/")[2] or "this folder"
            self.changed.emit(
                f"Archives in {where} will be read whole ({pattern} in Keep configuration "
                "→ Folders → Read inside archives): scanning to read them."
            )
            self.scan_requested.emit([root_id])

        run_in_pool(job, on_done=done, on_error=self._failed, pool=self._pool)

    # --- the dialog ---

    def _open_dialog(self, current: ArchiveChoices) -> None:
        base = self.session.root_path(current.root_id)
        theme = self.session.theme
        dialog = ChooseInsideDialog(
            current,
            local_path(base, current.relpath),
            theme.extensions or None,
            self._pool,
            self._window,
        )
        dialog.loaded.connect(lambda: self._loaded(dialog))
        dialog.accepted.connect(lambda: self._save(dialog))
        dialog.finished.connect(lambda _: self._closed(dialog))
        self.dialog = dialog
        dialog.open()

    def _loaded(self, dialog: ChooseInsideDialog) -> None:
        note = left_out_message(dialog.left_out)
        if note:
            self.message.emit(f"{note} (inside {dialog.current.name}).")

    def _save(self, dialog: ChooseInsideDialog) -> None:
        current, shown, hidden = dialog.current, dialog.shown(), dialog.hidden()
        if dialog.error is not None or not dialog.files:
            return
        count = len(hidden)
        what = (
            f"{count:,} file{'' if count == 1 else 's'} hidden, keeping their tags; "
            if count
            else ""
        )
        self._write(
            lambda conn: save_choices(conn, current, shown, hidden),
            f"Saved what's read inside {current.name}: {what}scanning to read the others.",
            current.root_id,
        )

    def _closed(self, dialog: ChooseInsideDialog) -> None:
        if self.dialog is dialog:
            self.dialog = None
        dialog.deleteLater()

    # --- running ---

    def _with_choices(self, archive_id: int, then: Callable[[ArchiveChoices], None]) -> None:
        session = self.session

        def job() -> ArchiveChoices | None:
            with session.reader.connect() as conn:
                return archive_choices(conn, archive_id, session.keep.config.roots)

        def done(current: ArchiveChoices | None) -> None:
            if not shiboken6.isValid(self):
                return
            if current is None:
                self.message.emit("That isn't an archive Tagalot can read inside.")
            else:
                then(current)

        run_in_pool(job, on_done=done, on_error=self._failed, pool=self._pool)

    def _write(self, work: Callable[..., object], text: str, scan: str | None = None) -> None:
        writer = self.session.writer

        def done(_: object) -> None:
            if not shiboken6.isValid(self):
                return
            self.changed.emit(text)
            if scan is not None:
                self.scan_requested.emit([scan])

        run_in_pool(lambda: writer.run(work), on_done=done, on_error=self._failed, pool=self._pool)

    def _failed(self, error: BaseException) -> None:
        if shiboken6.isValid(self):
            self.message.emit(f"Couldn't change what's read inside the archive: {error}")


def may_be_archive(schema: ThemeSchema, type_id: str) -> bool:
    """Whether an item of this type can have an archive as its file (its primary role takes
    archives), so its menu offers what to read inside it."""
    try:
        entity = schema.by_type_id(type_id).entity
    except KeyError:
        return False
    primary = next((r for r in entity.roles if r.primary), None)
    return primary is not None and bool({Kind.ARCHIVE, Kind.ANY} & set(primary.kinds))


class InsideArchiveMenu(QMenu):
    """ "Inside the archive", on a search result: filled when first shown, once the item's
    file is found, with the archive's commands (or why there are none)."""

    def __init__(
        self, opener: FileOpener, actions: ArchiveActions, entity_id: int, parent: QWidget
    ) -> None:
        super().__init__("Inside the archive", parent)
        self.setObjectName("inside_archive")
        self._opener = opener
        self._actions = actions
        self._entity_id = entity_id
        self._filled = False
        self.addAction("Looking for the file…").setEnabled(False)
        self.aboutToShow.connect(self.fill)

    def fill(self) -> None:
        """Look the file up (once) and list the commands for it."""
        if self._filled:
            return
        self._filled = True
        self._opener.lookup(self._entity_id, None, self._found)

    def _found(self, file: FileToOpen | CannotOpen) -> None:
        if not shiboken6.isValid(self):
            return
        self.clear()
        if isinstance(file, CannotOpen):
            self.addAction(str(file)).setEnabled(False)
        elif file.member or file.is_dir or file.ext not in ARCHIVE_EXTENSIONS:
            self.addAction("Its file isn't an archive").setEnabled(False)
        else:
            self._actions.add_archive_actions(self, file.resource_id)
