"""The Triage page: what needs attention (DESIGN.md §12 "Triage").

Three tabs, each with how many it holds:

- **Unlinked files:** files no item uses (a table, the first :data:`MAX_FILES`), with Open
  file, Show in file manager, Skip in scans (an exact exclude pattern on its root), and
  Dismiss.
- **Untagged items:** a search page (``SearchSpec(triage="untagged")``): tag them with the
  Tags panel or by dropping tags, or Dismiss them.
- **Missing files:** items whose files are all missing, a search page too, with Delete…
  and Show where they were.

The window builds the search pages (``make_search``), so they work like any other, and
handles the requests: dismissing and deleting are undo steps (``TagActions``).
"""

import os
from collections.abc import Callable
from dataclasses import dataclass

import shiboken6
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.formats import format_bytes
from tagalot.core.handlers import OPEN, REVEAL, FileToOpen, files_to_open
from tagalot.core.models import ResourceStatus
from tagalot.core.roots import local_path
from tagalot.core.search import count_matches
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.triage import (
    MISSING,
    UNLINKED,
    UNTAGGED,
    UnlinkedFile,
    count_unlinked,
    unlinked_files,
)
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import run_in_pool

MAX_FILES = 10_000
"""Unlinked files listed at once (the count shows them all)."""

UNLINKED_TAB, UNTAGGED_TAB, MISSING_TAB = 0, 1, 2
_TITLES = {
    UNLINKED_TAB: "Unlinked files",
    UNTAGGED_TAB: "Untagged items",
    MISSING_TAB: "Missing files",
}

ModelIndex = QModelIndex | QPersistentModelIndex


@dataclass(frozen=True)
class Counts:
    unlinked: int
    untagged: int
    missing: int


class UnlinkedModel(QAbstractTableModel):
    """The unlinked files: folder, path, size, status."""

    HEADERS = ("Folder", "Path", "Size", "Status")

    def __init__(self) -> None:
        super().__init__()
        self.files: list[UnlinkedFile] = []

    def set_files(self, files: list[UnlinkedFile]) -> None:
        self.beginResetModel()
        self.files = files
        self.endResetModel()

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.files)

    def columnCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        file = self.files[index.row()]
        column = index.column()
        if column == 0:
            return file.root_name
        if column == 1:
            return file.relpath
        if column == 2:
            return format_bytes(file.size) if file.size is not None else ""
        return "offline" if file.status is ResourceStatus.OFFLINE else ""


class TriagePage(QWidget):
    """See the module docstring. Signals carry requests for the window to carry out."""

    dismiss_requested = Signal(str, list)
    """(list name, ids): hide them until they change."""
    delete_requested = Signal(list)
    """Entity ids to delete (the window asks first)."""
    link_requested = Signal(list)
    """``UnlinkedFile`` values to link to an item by hand."""
    skip_requested = Signal(list)
    """``UnlinkedFile`` values to leave out of scans."""
    message = Signal(str)

    def __init__(
        self,
        session: KeepSession,
        make_search: Callable[[str, SearchSpec, str], SearchPage],
        file_opener: FileOpener | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.file_opener = file_opener
        self.counts: Counts | None = None

        self.files = UnlinkedModel()
        self.table = QTableView()
        self.table.setObjectName("unlinked")
        self.table.setModel(self.files)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(lambda _: self._open_selected(OPEN))
        self.files_note = QLabel()
        self.files_note.setWordWrap(True)
        open_file = QPushButton("Open file")
        open_file.clicked.connect(lambda: self._open_selected(OPEN))
        reveal = QPushButton("Show in file manager")
        reveal.clicked.connect(lambda: self._open_selected(REVEAL))
        self.skip_button = QPushButton("Skip in scans…")
        self.skip_button.setToolTip(
            "Add an exclude pattern for each selected file to its folder's settings, so scans "
            "leave it out (Keep configuration → Folders → Skip)"
        )
        self.skip_button.clicked.connect(self._skip)
        link = QPushButton("Link to item\u2026")
        link.setToolTip("Make the selected files an item's, in one of its roles (Edit → Undo)")
        link.clicked.connect(self._link)
        dismiss_files = QPushButton("Dismiss")
        dismiss_files.setToolTip("Hide the selected files here until they change")
        dismiss_files.clicked.connect(self._dismiss_files)
        files_tab = _tab(
            self.table, [open_file, reveal, link, self.skip_button, dismiss_files], self.files_note
        )

        self.untagged = make_search(
            "Untagged items", SearchSpec(triage=UNTAGGED), "triage:untagged"
        )
        dismiss_items = QPushButton("Dismiss")
        dismiss_items.setToolTip("Hide the selected items here until they're edited or tagged")
        dismiss_items.clicked.connect(self._dismiss_items)
        untagged_tab = _tab(self.untagged, [dismiss_items])

        self.missing = make_search("Missing files", SearchSpec(triage=MISSING), "triage:missing")
        delete = QPushButton("Delete…")
        delete.setToolTip("Delete the selected items and their tags (never files); Edit → Undo")
        delete.clicked.connect(self._delete)
        where = QPushButton("Show where they were")
        where.setToolTip("Open the folder the selected item's file was in, to look for it")
        where.clicked.connect(self._show_where)
        missing_tab = _tab(self.missing, [delete, where])

        self.tabs = QTabWidget()
        self.tabs.addTab(files_tab, _TITLES[UNLINKED_TAB])
        self.tabs.addTab(untagged_tab, _TITLES[UNTAGGED_TAB])
        self.tabs.addTab(missing_tab, _TITLES[MISSING_TAB])
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        self.refresh()

    # --- showing ---

    def refresh(self) -> None:
        """Count every list and load the unlinked files again, in a worker (the search
        tabs refresh with the window's other searches)."""
        session = self.session

        def job() -> tuple[Counts, list[UnlinkedFile]]:
            tree = session.tag_cache.get()
            with session.reader.connect() as conn:
                counts = Counts(
                    count_unlinked(conn),
                    count_matches(conn, SearchSpec(triage=UNTAGGED), tree),
                    count_matches(conn, SearchSpec(triage=MISSING), tree),
                )
                return counts, unlinked_files(conn, MAX_FILES)

        def done(result: tuple[Counts, list[UnlinkedFile]]) -> None:
            if shiboken6.isValid(self):
                self._show(*result)

        run_in_pool(job, on_done=done)

    def _show(self, counts: Counts, files: list[UnlinkedFile]) -> None:
        self.counts = counts
        for tab, n in (
            (UNLINKED_TAB, counts.unlinked),
            (UNTAGGED_TAB, counts.untagged),
            (MISSING_TAB, counts.missing),
        ):
            self.tabs.setTabText(tab, f"{_TITLES[tab]} ({n:,})")
        self.files.set_files(files)
        if counts.unlinked > len(files):
            self.files_note.setText(f"Showing the first {len(files):,} of {counts.unlinked:,}.")
        elif not files:
            self.files_note.setText("Every file is used by an item.")
        else:
            self.files_note.setText(
                "Files no item uses: the theme didn't recognize them, or they're extras "
                "(a cover image is read from its folder without being linked)."
            )

    def current_search(self) -> SearchPage | None:
        """The search page of the current tab (tagging applies to its selection)."""
        index = self.tabs.currentIndex()
        return {UNTAGGED_TAB: self.untagged, MISSING_TAB: self.missing}.get(index)

    def selected_files(self) -> list[UnlinkedFile]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.files.files[r] for r in rows]

    # --- requests ---

    def _open_selected(self, how: str) -> None:
        files = self.selected_files()
        if not files or self.file_opener is None:
            return
        file = files[0]
        try:
            base = self.session.root_path(file.root_id)
        except StopIteration:
            self.message.emit(f"{file.root_name} has no folder on this computer.")
            return
        self.file_opener.act(
            FileToOpen(file.resource_id, local_path(base, file.relpath), False), how
        )

    def _link(self) -> None:
        files = self.selected_files()
        if files:
            self.link_requested.emit(files)

    def _skip(self) -> None:
        files = self.selected_files()
        if files:
            self.skip_requested.emit(files)

    def _dismiss_files(self) -> None:
        files = self.selected_files()
        if files:
            self.dismiss_requested.emit(UNLINKED, [f.resource_id for f in files])

    def _dismiss_items(self) -> None:
        self.untagged.selected_entity_ids(
            lambda ids: self.dismiss_requested.emit(UNTAGGED, ids) if ids else None
        )

    def _delete(self) -> None:
        self.missing.selected_entity_ids(
            lambda ids: self.delete_requested.emit(ids) if ids else None
        )

    def _show_where(self) -> None:
        session = self.session

        def chosen(ids: list[int]) -> None:
            if not ids:
                return

            def job() -> str:
                with session.reader.connect() as conn:
                    files = files_to_open(conn, session.schema, ids[0], _known(session))
                paths = [f.path for f in files if f.path]
                if not paths:
                    return ""
                folder = os.path.dirname(paths[0])
                return folder if os.path.isdir(folder) else f"!{folder}"

            run_in_pool(job, on_done=self._found_folder)

        self.missing.selected_entity_ids(chosen)

    def _found_folder(self, folder: str) -> None:
        if not shiboken6.isValid(self):
            return
        if not folder:
            self.message.emit("Its root has no folder on this computer.")
        elif folder.startswith("!"):
            self.message.emit(f"Its folder is gone too: {folder[1:]}")
        elif self.file_opener is not None:
            self.file_opener.act(FileToOpen(0, folder, True), OPEN)


def _known(session: KeepSession) -> Callable[[str], str | None]:
    def path(root_id: str) -> str | None:
        try:
            return session.root_path(root_id)
        except StopIteration:
            return None

    return path


def _tab(main: QWidget, buttons: list[QPushButton], note: QLabel | None = None) -> QWidget:
    row = QHBoxLayout()
    if note is not None:
        row.addWidget(note, 1)
    else:
        row.addStretch(1)
    for button in buttons:
        row.addWidget(button)
    tab = QWidget()
    column = QVBoxLayout(tab)
    column.addLayout(row)
    column.addWidget(main, 1)
    return tab


__all__ = ["UNLINKED", "TriagePage"]
