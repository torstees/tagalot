"""The Triage page: what needs attention (DESIGN.md §12 "Triage").

Tabs, each with how many it holds:

- **Unlinked files:** files no item uses (a table, the first :data:`MAX_FILES`), with Open
  file, Show in file manager, Skip in scans (an exact exclude pattern on its root), and
  Dismiss.
- **Untagged items:** a search page (``SearchSpec(triage="untagged")``): tag them with the
  Tags panel or by dropping tags, or Dismiss them.
- **Missing files:** items whose files are all missing, a search page too, with Delete…
  and Show where they were, both also in an item's right-click menu.
- **Unmatched keywords:** items with file keywords no tag matches (#295).
- **Markdown:** the notes folder's notes that aren't an item's note (#382): matching no
  item, several, or an item with another note; and Markdown files whose front matter gives
  a key twice or doesn't parse (#438); with Open file and Show in file manager.
- **Pictures:** items' pictures not found (a Markdown book's ``cover``), with the paths
  tried and what is there now, and pictures that can't be drawn (#426); with Open page,
  Show the file, Re-read from file, and Ignore (for the item, the value everywhere, or
  every item of the type); Show ignored lists ignored rows, to stop ignoring them.
- **New in archives:** archives read member by member that hold files not decided on
  (#520, ``core.new_in_archives``), each with how many and of which kinds, the most
  recently changed first; Review… (or a double-click) opens Choose what to read inside…,
  where they're marked new and start hidden, and Hide all new records them hidden.
- **Skipped:** each rule that skips files now (a Skip pattern, the notes folder, what the
  theme doesn't read, archive members not read), with how many files and how many items
  it hides (#527, ``core.skip_reasons``); selecting one lists its files and says where the
  rule lives.
- **Links:** broken links and shortcuts (#604, ``core.broken_links``), with where they
  pointed, and files in linked folders (#601, ``core.linked_folders``) that differ from
  the file at the same path in the folder they're linked with, so they aren't linked;
  with Open file and Open the other (for a file that differs) and Show in file manager.

The window builds the search pages (``make_search``), so they work like any other, and
handles the requests: dismissing and deleting are undo steps (``TagActions``).
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import shiboken6
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QStackedWidget,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import Connection

from tagalot.core.broken_links import Broken, broken_links
from tagalot.core.formats import format_bytes
from tagalot.core.handlers import OPEN, REVEAL, FileToOpen, files_to_open
from tagalot.core.linked_folders import FolderDifference, folder_differences
from tagalot.core.markdown_check import MarkdownFileProblem, markdown_problems
from tagalot.core.models import ResourceStatus
from tagalot.core.new_in_archives import NewInArchive, count_new_in_archives, new_in_archives
from tagalot.core.picture_problems import (
    CANT_DRAW,
    ITEM,
    NOT_FOUND,
    TYPE,
    VALUE,
    PictureProblem,
    PictureScope,
    picture_problems,
)
from tagalot.core.roots import local_path
from tagalot.core.search import count_matches
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.skip_reasons import SkippedFile, SkippedReport, SkipRule, ThemeScope, skip_reasons
from tagalot.core.triage import (
    KEYWORDS,
    MISSING,
    UNLINKED,
    UNTAGGED,
    NoteProblem,
    UnlinkedFile,
    count_unlinked,
    note_problems,
    unlinked_files,
)
from tagalot.themes.api import entity_label
from tagalot.ui.archive_actions import ArchiveActions
from tagalot.ui.empty_state import EmptyState
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import run_in_pool

MAX_FILES = 10_000
"""Unlinked files listed at once (the count shows them all)."""

(
    UNLINKED_TAB,
    UNTAGGED_TAB,
    MISSING_TAB,
    KEYWORDS_TAB,
    MARKDOWN_TAB,
    PICTURES_TAB,
    NEW_TAB,
    SKIPPED_TAB,
    LINKS_TAB,
) = range(9)
_TITLES = {
    UNLINKED_TAB: "Unlinked files",
    UNTAGGED_TAB: "Untagged items",
    MISSING_TAB: "Missing files",
    KEYWORDS_TAB: "Unmatched keywords",
    MARKDOWN_TAB: "Markdown",
    PICTURES_TAB: "Pictures",
    NEW_TAB: "New in archives",
    SKIPPED_TAB: "Skipped",
    LINKS_TAB: "Links",
}
PICTURES_HINT = "Select a row to see where Tagalot looked."
SKIPPED_HINT = "Select a rule to see its files and where it lives."
IGNORED_AS = {
    ITEM: "ignored for this item",
    VALUE: "ignored in every file",
    TYPE: "ignored for its type",
}

ModelIndex = QModelIndex | QPersistentModelIndex


@dataclass(frozen=True)
class Counts:
    unlinked: int
    untagged: int
    missing: int
    keywords: int = 0
    notes: int = 0
    pictures: int = 0
    skipped: int = 0
    new_in_archives: int = 0
    links: int = 0


MarkdownRow = NoteProblem | MarkdownFileProblem
"""A row of the Markdown tab: a note that isn't an item's note, or a file whose front
matter has problems (#438)."""


Loaded = tuple[
    Counts,
    list[UnlinkedFile],
    list[MarkdownRow],
    list[PictureProblem],
    SkippedReport,
    list[NewInArchive],
    list["LinkRow"],
]
"""What :meth:`TriagePage.refresh` loads in its worker."""


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
            return file.shown
        if column == 2:
            return format_bytes(file.size) if file.size is not None else ""
        return "offline" if file.status is ResourceStatus.OFFLINE else ""


class MarkdownModel(QAbstractTableModel):
    """Markdown files that need attention: path, and why."""

    HEADERS = ("File", "Problem")

    def __init__(self) -> None:
        super().__init__()
        self.notes: list[MarkdownRow] = []

    def set_notes(self, notes: list[MarkdownRow]) -> None:
        self.beginResetModel()
        self.notes = notes
        self.endResetModel()

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.notes)

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
        note = self.notes[index.row()]
        if index.column() == 0:
            return note.relpath
        offline = " (offline)" if note.status is ResourceStatus.OFFLINE else ""
        return note.reason + offline


class PicturesModel(QAbstractTableModel):
    """Items' pictures not found or that can't be drawn: item, type, problem, file."""

    HEADERS = ("Item", "Type", "Problem", "File")

    def __init__(self, type_labels: dict[str, str]) -> None:
        super().__init__()
        self.type_labels = type_labels
        self.problems: list[PictureProblem] = []

    def set_problems(self, problems: list[PictureProblem]) -> None:
        self.beginResetModel()
        self.problems = problems
        self.endResetModel()

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.problems)

    def columnCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        problem = self.problems[index.row()]
        if role == Qt.ItemDataRole.ForegroundRole and problem.ignored:
            return QPalette().color(QPalette.ColorRole.PlaceholderText)
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        summary = problem.summary
        if problem.ignored:
            summary += f" ({IGNORED_AS[problem.ignored]})"
        return (
            problem.title,
            self.type_labels.get(problem.type, problem.type),
            summary,
            problem.relpath,
        )[index.column()]


class SkipRulesModel(QAbstractTableModel):
    """The rules that skip files: rule, folder, files, items hidden (#527)."""

    HEADERS = ("Rule", "Folder", "Files", "Items hidden")

    def __init__(self) -> None:
        super().__init__()
        self.rules: list[SkipRule] = []

    def set_rules(self, rules: list[SkipRule]) -> None:
        self.beginResetModel()
        self.rules = rules
        self.endResetModel()

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rules)

    def columnCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        rule = self.rules[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return rule.where
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() >= 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return (
            rule.title,
            rule.root_name or "Every folder",
            f"{rule.count:,}",
            f"{rule.hidden:,}" if rule.hidden else "",
        )[index.column()]


class SkippedFilesModel(QAbstractTableModel):
    """One rule's files: folder and path."""

    HEADERS = ("Folder", "Path")

    def __init__(self) -> None:
        super().__init__()
        self.files: list[SkippedFile] = []
        self.root_names: dict[str, str] = {}

    def set_files(self, files: list[SkippedFile], root_names: dict[str, str]) -> None:
        self.beginResetModel()
        self.files = files
        self.root_names = root_names
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
        if index.column() == 0:
            return self.root_names.get(file.root_id, file.root_id)
        return file.shown


LinkRow = Broken | FolderDifference
"""A row of the Links tab: a broken link (#604), or a file that differs in linked folders
(#601)."""


class LinksModel(QAbstractTableModel):
    """Broken links, then files in linked folders that differ: folder, path, problem."""

    HEADERS = ("Folder", "Path", "Problem")

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[LinkRow] = []

    def set_rows(self, rows: list[LinkRow]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rows)

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
        row = self.rows[index.row()]
        if isinstance(row, Broken):
            problem = f"Broken: pointed to {row.pointed}"
        else:
            problem = f"Differs from {row.first_shown}"
        return (row.root_name, row.relpath, problem)[index.column()]


class NewInArchivesModel(QAbstractTableModel):
    """Archives with new files to review (#520): archive, folder, what's new, changed."""

    HEADERS = ("Archive", "Folder", "New", "Changed")

    def __init__(self) -> None:
        super().__init__()
        self.archives: list[NewInArchive] = []
        self.root_names: dict[str, str] = {}

    def set_archives(self, archives: list[NewInArchive], root_names: dict[str, str]) -> None:
        self.beginResetModel()
        self.archives = archives
        self.root_names = root_names
        self.endResetModel()

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.archives)

    def columnCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        archive = self.archives[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            shown = archive.paths[:20]
            more = archive.count - len(shown)
            return "\n".join([archive.relpath, *shown] + ([f"and {more:,} more"] if more else []))
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return (
            archive.relpath,
            self.root_names.get(archive.root_id, archive.root_id),
            archive.summary,
            _when(archive.mtime_ns),
        )[index.column()]


def _when(mtime_ns: int | None) -> str:
    if mtime_ns is None:
        return ""
    return datetime.fromtimestamp(mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M")


def skip_rule_details(rule: SkipRule) -> str:
    """What the details under the Skipped tab's rules say about one."""
    files = "1 file" if rule.count == 1 else f"{rule.count:,} files"
    lines = [f"{rule.title}: {files}."]
    if rule.hidden:
        items = "1 item is" if rule.hidden == 1 else f"{rule.hidden:,} items are"
        lines.append(f"{items} hidden because all of their files are skipped.")
    lines.append(rule.where)
    if rule.count > len(rule.files):
        lines.append(f"Showing the first {len(rule.files):,}.")
    return " ".join(lines)


def picture_details(problem: PictureProblem) -> str:
    """What the details under the Pictures list say about one row."""
    name = problem.relpath.rpartition("/")[2]
    if problem.kind == CANT_DRAW:
        return (
            f"{name} can't be drawn: {problem.reason}\n"
            "Replace the picture or save it again in a common format (JPEG, PNG); it's "
            "listed until the file changes."
        )
    lines = [f"{name} names its {problem.role}: {problem.written}"]
    if not problem.tried:
        lines.append(problem.reason or "")
    else:
        lines.append("Looked in, in order:")
        lines.extend(f"{i}. {t.relpath}: {t.state}" for i, t in enumerate(problem.tried, 1))
        if not any(t.found for t in problem.tried):
            lines.append(
                "Fix the name in the file, put the picture in one of these places, or set "
                "an assets folder (the item's Assets folder field, or Keep configuration)."
            )
    return "\n".join(lines)


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
    keywords_requested = Signal()
    """Open TOOLS → File keywords (#295)."""
    open_requested = Signal(int)
    """An item's page to open."""
    reread_requested = Signal(list)
    """Entity ids to read from their files again."""
    ignore_requested = Signal(list, str, str)
    """(Pictures rows, scope, label): leave them off the list (#426)."""
    stop_ignoring_requested = Signal(object, str)
    """(a Pictures row, label): list it again."""

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
        self.archive_actions: ArchiveActions | None = None
        """The window's archive commands: New in archives' Review… and Hide all new."""
        self.counts: Counts | None = None
        self._refreshes = 0
        """How many refreshes have started; a result shows only if it is the latest's."""

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
        self.files_empty = EmptyState("Nothing to tidy here", "Every file is used by an item.")
        self.files_stack = QStackedWidget()
        self.files_stack.addWidget(self.table)
        self.files_stack.addWidget(self.files_empty)
        files_tab = _tab(
            self.files_stack,
            [open_file, reveal, link, self.skip_button, dismiss_files],
            self.files_note,
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
        self.missing.add_menu_action("Delete…", self._delete_items)
        self.missing.add_menu_action("Show where it was", self._show_where_of)

        self.keywords = make_search(
            "Unmatched keywords", SearchSpec(triage=KEYWORDS), "triage:keywords"
        )
        open_keywords = QPushButton("File keywords…")
        open_keywords.setToolTip(
            "Map these items' keywords to tags, create tags for them, or ignore them"
        )
        open_keywords.clicked.connect(self.keywords_requested.emit)
        keywords_tab = _tab(self.keywords, [open_keywords])

        self.notes = MarkdownModel()
        self.notes_table = QTableView()
        self.notes_table.setObjectName("markdown")
        self.notes_table.setModel(self.notes)
        self.notes_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.notes_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.notes_table.verticalHeader().hide()
        header = self.notes_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.notes_table.doubleClicked.connect(lambda _: self._open_note(OPEN))
        open_note = QPushButton("Open file")
        open_note.setToolTip("Open it in your Markdown editor, to fix it")
        open_note.clicked.connect(lambda: self._open_note(OPEN))
        reveal_note = QPushButton("Show in file manager")
        reveal_note.clicked.connect(lambda: self._open_note(REVEAL))
        self.notes_note = QLabel(
            "Notes in the notes folder that aren't an item's note (a note is matched by its "
            "title or file name and aliases, and typed by tagalot-type or its folder), and "
            "Markdown files whose front matter gives a key twice or doesn't parse."
        )
        self.notes_note.setWordWrap(True)
        self.notes_empty = EmptyState(
            "Nothing to fix in your Markdown",
            "Every note found its item, and every file's front matter reads cleanly.",
        )
        self.notes_stack = QStackedWidget()
        self.notes_stack.addWidget(self.notes_table)
        self.notes_stack.addWidget(self.notes_empty)
        notes_tab = _tab(self.notes_stack, [open_note, reveal_note], self.notes_note)

        labels = {t.type_id: entity_label(t.entity) for t in session.schema.entities.values()}
        self.pictures = PicturesModel(labels)
        self.pictures_table = QTableView()
        self.pictures_table.setObjectName("pictures")
        self.pictures_table.setModel(self.pictures)
        self.pictures_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pictures_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.pictures_table.verticalHeader().hide()
        header = self.pictures_table.horizontalHeader()
        for column in (0, 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.pictures_table.doubleClicked.connect(lambda _: self._open_picture_item())
        self.pictures_table.selectionModel().selectionChanged.connect(
            lambda *_: self._picture_selection_changed()
        )
        self.picture_details = QLabel(PICTURES_HINT)
        self.picture_details.setObjectName("picture_details")
        self.picture_details.setWordWrap(True)
        self.picture_details.setTextFormat(Qt.TextFormat.PlainText)
        self.picture_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.picture_details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.picture_details.setMinimumHeight(self.fontMetrics().lineSpacing() * 6)
        listed = QWidget()
        listed_layout = QVBoxLayout(listed)
        listed_layout.setContentsMargins(0, 0, 0, 0)
        listed_layout.addWidget(self.pictures_table, 1)
        listed_layout.addWidget(self.picture_details)
        open_item = QPushButton("Open page")
        open_item.clicked.connect(self._open_picture_item)
        show_file = QPushButton("Show the file")
        show_file.setToolTip(
            "Show the file that names the picture, or the picture that can't be drawn, in "
            "your file manager"
        )
        show_file.clicked.connect(self._show_picture_file)
        reread = QPushButton("Re-read from file")
        reread.setToolTip("Read the item's files again, after fixing the file or a setting")
        reread.clicked.connect(self._reread_picture_item)
        self.ignore_button = QPushButton("Ignore")
        self.ignore_button.setToolTip("Leave the selected rows off this list (Edit → Undo)")
        self.ignore_menu = QMenu(self.ignore_button)
        self.ignore_item = self.ignore_menu.addAction("For this item")
        self.ignore_item.triggered.connect(lambda: self._ignore_pictures(ITEM))
        self.ignore_value = self.ignore_menu.addAction("This value in every file")
        self.ignore_value.triggered.connect(lambda: self._ignore_pictures(VALUE))
        self.ignore_type = self.ignore_menu.addAction("Every item of this type")
        self.ignore_type.triggered.connect(lambda: self._ignore_pictures(TYPE))
        self.ignore_button.setMenu(self.ignore_menu)
        self.stop_ignoring_button = QPushButton("Stop ignoring")
        self.stop_ignoring_button.setToolTip("List the selected ignored row again")
        self.stop_ignoring_button.clicked.connect(self._stop_ignoring_picture)
        self.show_ignored = QCheckBox("Show ignored")
        self.show_ignored.setToolTip("List the rows you ignored too, greyed, to stop ignoring them")
        self.show_ignored.toggled.connect(lambda _: self.refresh())
        self.pictures_note = QLabel(
            "Pictures items' files name that weren't found, and pictures that can't be drawn."
        )
        self.pictures_note.setWordWrap(True)
        self.pictures_empty = EmptyState(
            "Every picture is in place", "Pictures items' files name were found and can be drawn."
        )
        self.pictures_stack = QStackedWidget()
        self.pictures_stack.addWidget(listed)
        self.pictures_stack.addWidget(self.pictures_empty)
        pictures_tab = _tab(
            self.pictures_stack,
            [
                self.show_ignored,
                open_item,
                show_file,
                reread,
                self.ignore_button,
                self.stop_ignoring_button,
            ],
            self.pictures_note,
        )
        self._picture_selection_changed()

        self.skip_rules = SkipRulesModel()
        self.skip_rules_table = QTableView()
        self.skip_rules_table.setObjectName("skip_rules")
        self.skip_rules_table.setModel(self.skip_rules)
        self.skip_rules_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.skip_rules_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.skip_rules_table.verticalHeader().hide()
        header = self.skip_rules_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.skip_rules_table.selectionModel().selectionChanged.connect(
            lambda *_: self._skip_rule_selected()
        )
        self.skip_details = QLabel(SKIPPED_HINT)
        self.skip_details.setObjectName("skip_details")
        self.skip_details.setWordWrap(True)
        self.skip_details.setTextFormat(Qt.TextFormat.PlainText)
        self.skip_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.skipped_files = SkippedFilesModel()
        self.skipped_files_table = QTableView()
        self.skipped_files_table.setObjectName("skipped_files")
        self.skipped_files_table.setModel(self.skipped_files)
        self.skipped_files_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.skipped_files_table.verticalHeader().hide()
        header = self.skipped_files_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        rules = QWidget()
        rules_layout = QVBoxLayout(rules)
        rules_layout.setContentsMargins(0, 0, 0, 0)
        rules_layout.addWidget(self.skip_rules_table, 1)
        rules_layout.addWidget(self.skip_details)
        rules_layout.addWidget(self.skipped_files_table, 2)
        self.skipped_note = QLabel(
            "Files scans leave out, by the rule that leaves them out. An item whose files are "
            "all skipped is hidden until one of them is read again."
        )
        self.skipped_note.setWordWrap(True)
        self.skipped_empty = EmptyState(
            "Nothing is skipped", "Scans read every file in your folders that the theme takes."
        )
        self.skipped_stack = QStackedWidget()
        self.skipped_stack.addWidget(rules)
        self.skipped_stack.addWidget(self.skipped_empty)
        skipped_tab = _tab(self.skipped_stack, [], self.skipped_note)

        self.new_archives = NewInArchivesModel()
        self.new_table = QTableView()
        self.new_table.setObjectName("new_in_archives")
        self.new_table.setModel(self.new_archives)
        self.new_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.new_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.new_table.verticalHeader().hide()
        header = self.new_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.new_table.doubleClicked.connect(lambda _: self.review())
        review = QPushButton("Review\u2026")
        review.setToolTip(
            "Choose what to read inside the selected archive: its new files are marked new "
            "and start hidden"
        )
        review.clicked.connect(self.review)
        hide_new = QPushButton("Hide all new")
        hide_new.setToolTip(
            "Leave the selected archives' new files out without looking (Choose what to read "
            "inside… shows them again)"
        )
        hide_new.clicked.connect(self.hide_new)
        self.new_note = QLabel(
            "Archives read file by file have new files, not read until you choose: review "
            "them, or hide them all."
        )
        self.new_note.setWordWrap(True)
        self.new_empty = EmptyState(
            "Nothing new inside archives",
            "Every file inside the archives read file by file has been decided on.",
        )
        self.new_stack = QStackedWidget()
        self.new_stack.addWidget(self.new_table)
        self.new_stack.addWidget(self.new_empty)
        new_tab = _tab(self.new_stack, [review, hide_new], self.new_note)

        self.links = LinksModel()
        self.links_table = QTableView()
        self.links_table.setObjectName("links")
        self.links_table.setModel(self.links)
        self.links_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.links_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.links_table.verticalHeader().hide()
        header = self.links_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in (1, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self.links_table.doubleClicked.connect(lambda _: self._open_link(OPEN, other=False))
        open_link = QPushButton("Open file")
        open_link.clicked.connect(lambda: self._open_link(OPEN, other=False))
        open_first = QPushButton("Open the other")
        open_first.setToolTip("Open the file it differs from, in the folder it's linked with")
        open_first.clicked.connect(lambda: self._open_link(OPEN, other=True))
        reveal_link = QPushButton("Show in file manager")
        reveal_link.clicked.connect(lambda: self._open_link(REVEAL, other=False))
        self.links_note = QLabel(
            "Links and shortcuts whose target is gone (deleted, renamed, moved, or on a drive "
            "that isn't connected): fix them on disk, and the next scan clears them. And files "
            "in linked folders that differ from the file at the same path in the other folder, "
            "so they aren't linked: make them the same, and the next scan links them."
        )
        self.links_note.setWordWrap(True)
        self.links_empty = EmptyState(
            "Every link works",
            "No link or shortcut is broken, and every file in a linked folder matches its "
            "other place.",
        )
        self.links_stack = QStackedWidget()
        self.links_stack.addWidget(self.links_table)
        self.links_stack.addWidget(self.links_empty)
        links_tab = _tab(self.links_stack, [open_link, open_first, reveal_link], self.links_note)

        self.tabs = QTabWidget()
        self.tabs.addTab(files_tab, _TITLES[UNLINKED_TAB])
        self.tabs.addTab(untagged_tab, _TITLES[UNTAGGED_TAB])
        self.tabs.addTab(missing_tab, _TITLES[MISSING_TAB])
        self.tabs.addTab(keywords_tab, _TITLES[KEYWORDS_TAB])
        self.tabs.addTab(notes_tab, _TITLES[MARKDOWN_TAB])
        self.tabs.addTab(pictures_tab, _TITLES[PICTURES_TAB])
        self.tabs.addTab(new_tab, _TITLES[NEW_TAB])
        self.tabs.addTab(skipped_tab, _TITLES[SKIPPED_TAB])
        self.tabs.addTab(links_tab, _TITLES[LINKS_TAB])
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        self.refresh()

    # --- showing ---

    def refresh(self) -> None:
        """Count every list and load the unlinked files again, in a worker (the search
        tabs refresh with the window's other searches)."""
        session = self.session

        known, show_ignored = _known(session), self.show_ignored.isChecked()
        config = session.keep.config
        notes = None if config.notes_root is None else (config.notes_root, config.notes_path)
        roots = list(config.roots)
        theme = session.theme
        scope = ThemeScope(theme.extensions or None, theme.dirs, session.see_through)

        def job() -> Loaded:
            tree = session.tag_cache.get()
            with session.reader.connect() as conn:
                pictures = picture_problems(conn, known, ignored=show_ignored)
                skipped = skip_reasons(conn, roots, scope, notes)
                links: list[LinkRow] = [*broken_links(conn), *folder_differences(conn)]
                counts = Counts(
                    count_unlinked(conn, notes),
                    count_matches(conn, SearchSpec(triage=UNTAGGED), tree),
                    count_matches(conn, SearchSpec(triage=MISSING), tree),
                    count_matches(conn, SearchSpec(triage=KEYWORDS), tree),
                    len(markdown := _markdown_rows(conn)),
                    sum(1 for p in pictures if not p.ignored),
                    skipped.total,
                    count_new_in_archives(conn),
                    len(links),
                )
                files = unlinked_files(conn, MAX_FILES, notes)
                new = new_in_archives(conn)
                return counts, files, markdown, pictures, skipped, new, links

        # Refreshes overlap (a change, then Show ignored): only the latest is shown, or an
        # older one finishing last would put back what it saw (#406).
        self._refreshes += 1
        refresh = self._refreshes

        def done(result: Loaded) -> None:
            if shiboken6.isValid(self) and refresh == self._refreshes:
                self._show(*result)

        run_in_pool(job, on_done=done)

    def _show(
        self,
        counts: Counts,
        files: list[UnlinkedFile],
        notes: list[MarkdownRow] | None = None,
        pictures: list[PictureProblem] | None = None,
        skipped: SkippedReport | None = None,
        new: list[NewInArchive] | None = None,
        links: list[LinkRow] | None = None,
    ) -> None:
        self.counts = counts
        for tab, n in (
            (UNLINKED_TAB, counts.unlinked),
            (UNTAGGED_TAB, counts.untagged),
            (MISSING_TAB, counts.missing),
            (KEYWORDS_TAB, counts.keywords),
            (MARKDOWN_TAB, counts.notes),
            (PICTURES_TAB, counts.pictures),
            (NEW_TAB, counts.new_in_archives),
            (SKIPPED_TAB, counts.skipped),
            (LINKS_TAB, counts.links),
        ):
            self.tabs.setTabText(tab, f"{_TITLES[tab]} ({n:,})")
        names = {r.id: r.name for r in self.session.keep.config.roots}
        self.links.set_rows(links or [])
        self.links_stack.setCurrentWidget(self.links_table if links else self.links_empty)
        self.new_archives.set_archives(new or [], names)
        self.new_stack.setCurrentWidget(self.new_table if new else self.new_empty)
        self._show_skipped(skipped.rules if skipped is not None else [])
        self.pictures.set_problems(pictures or [])
        self.pictures_stack.setCurrentIndex(0 if pictures else 1)
        self._picture_selection_changed()
        self.notes.set_notes(notes or [])
        self.notes_stack.setCurrentWidget(self.notes_table if notes else self.notes_empty)
        self.files.set_files(files)
        self.files_stack.setCurrentWidget(self.table if files else self.files_empty)
        if counts.unlinked > len(files):
            self.files_note.setText(f"Showing the first {len(files):,} of {counts.unlinked:,}.")
        elif not files:
            self.files_note.setText("Every file is used by an item.")
        else:
            self.files_note.setText(
                "Files no item uses: the theme didn't recognize them, or they're extras "
                "(a cover image is read from its folder without being linked)."
            )

    def _show_skipped(self, rules: list[SkipRule]) -> None:
        """Show the rules, keeping the one selected when it's still there."""
        chosen = self.selected_skip_rule()
        key = None if chosen is None else (chosen.kind, chosen.root_name, chosen.value)
        self.skip_rules.set_rules(rules)
        self.skipped_stack.setCurrentIndex(0 if rules else 1)
        for row, rule in enumerate(rules):
            if (rule.kind, rule.root_name, rule.value) == key:
                self.skip_rules_table.selectRow(row)
                break
        self._skip_rule_selected()

    def selected_skip_rule(self) -> SkipRule | None:
        rows = self.skip_rules_table.selectionModel().selectedRows()
        return self.skip_rules.rules[rows[0].row()] if rows else None

    def _skip_rule_selected(self) -> None:
        rule = self.selected_skip_rule()
        names = {r.id: r.name for r in self.session.keep.config.roots}
        self.skipped_files.set_files(rule.files if rule is not None else [], names)
        self.skip_details.setText(SKIPPED_HINT if rule is None else skip_rule_details(rule))

    def selected_new(self) -> list[NewInArchive]:
        """The archives selected in New in archives."""
        rows = sorted(i.row() for i in self.new_table.selectionModel().selectedRows())
        return [self.new_archives.archives[r] for r in rows]

    def review(self) -> None:
        """Choose what to read inside the selected archive (the first, if several)."""
        chosen = self.selected_new()
        if not chosen:
            self.message.emit("Select an archive to review.")
        elif self.archive_actions is not None:
            self.archive_actions.choose(chosen[0].archive_id)

    def hide_new(self) -> None:
        """Hide all new: record the selected archives' new files hidden."""
        chosen = self.selected_new()
        if not chosen:
            self.message.emit("Select the archives whose new files to hide.")
        elif self.archive_actions is not None:
            self.archive_actions.hide_new(chosen)

    def current_search(self) -> SearchPage | None:
        """The search page of the current tab (tagging applies to its selection)."""
        index = self.tabs.currentIndex()
        return {
            UNTAGGED_TAB: self.untagged,
            MISSING_TAB: self.missing,
            KEYWORDS_TAB: self.keywords,
        }.get(index)

    def selected_files(self) -> list[UnlinkedFile]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.files.files[r] for r in rows]

    # --- requests ---

    def _open_selected(self, how: str) -> None:
        files = self.selected_files()
        if not files or self.file_opener is None:
            return
        # Looked up by id: a file inside an archive opens from a copy (#524).
        self.file_opener.act_on_resource(files[0].resource_id, how)

    def selected_link(self) -> LinkRow | None:
        rows = self.links_table.selectionModel().selectedRows()
        return self.links.rows[rows[0].row()] if rows else None

    def _open_link(self, how: str, *, other: bool) -> None:
        """Open or show the selected row's file, or (``other``) the one it differs from. A
        broken link can only be shown in the file manager."""
        row = self.selected_link()
        if row is None or self.file_opener is None:
            return
        if isinstance(row, Broken):
            if how != REVEAL:
                self.message.emit(f"Its target is gone: it pointed to {row.pointed}.")
                return
            base = _known(self.session)(row.root_id)
            if base is None:
                self.message.emit("Its watched folder has no folder on this computer.")
                return
            self.file_opener.act(FileToOpen(0, local_path(base, row.relpath), False), how)
            return
        root_id, relpath = (
            (row.first_root_id, row.first_relpath) if other else (row.root_id, row.relpath)
        )
        base = _known(self.session)(root_id)
        if base is None:
            self.message.emit("Its watched folder has no folder on this computer.")
            return
        resource_id = row.first_id if other else row.resource_id
        self.file_opener.act(FileToOpen(resource_id, local_path(base, relpath), False), how)

    def selected_note(self) -> MarkdownRow | None:
        rows = self.notes_table.selectionModel().selectedRows()
        return self.notes.notes[rows[0].row()] if rows else None

    def _open_note(self, how: str) -> None:
        note = self.selected_note()
        if note is None or self.file_opener is None:
            return
        base = _known(self.session)(note.root_id)
        if base is None:
            self.message.emit("Its watched folder has no folder on this computer.")
            return
        self.file_opener.act(
            FileToOpen(note.resource_id, local_path(base, note.relpath), False), how
        )

    def selected_pictures(self) -> list[PictureProblem]:
        rows = sorted({i.row() for i in self.pictures_table.selectionModel().selectedRows()})
        return [self.pictures.problems[r] for r in rows]

    def selected_picture(self) -> PictureProblem | None:
        found = self.selected_pictures()
        return found[0] if found else None

    def _picture_selection_changed(self) -> None:
        """Show the first selected row's details, and name what Ignore and Stop ignoring
        would do."""
        chosen = self.selected_pictures()
        problem = chosen[0] if chosen else None
        self.picture_details.setText(PICTURES_HINT if problem is None else picture_details(problem))
        listed = [p for p in chosen if not p.ignored]
        self.ignore_button.setEnabled(bool(listed))
        self.ignore_item.setText("For this item" if len(listed) < 2 else "For these items")
        values = _values(listed)
        self.ignore_value.setEnabled(bool(values))
        self.ignore_value.setText(
            f"\u201c{values[0]}\u201d in every file"
            if len(values) == 1
            else "These values in every file"
        )
        types = self._type_names(listed)
        self.ignore_type.setText(
            f"Every {types[0]}" if len(types) == 1 else "Every item of these types"
        )
        self.stop_ignoring_button.setVisible(self.show_ignored.isChecked())
        self.stop_ignoring_button.setEnabled(problem is not None and problem.ignored is not None)

    def _type_names(self, problems: list[PictureProblem]) -> list[str]:
        labels = self.pictures.type_labels
        return list(dict.fromkeys(labels.get(p.type, p.type) for p in problems))

    def _ignore_pictures(self, scope: PictureScope) -> None:
        listed = [p for p in self.selected_pictures() if not p.ignored]
        if scope == VALUE:
            listed = [p for p in listed if p.kind == NOT_FOUND and p.written]
        if not listed:
            return
        if scope == ITEM:
            first = listed[0]
            what = f"missing {first.role}" if first.kind == NOT_FOUND else "damaged picture"
            label = (
                f"Ignore {first.title}'s {what}"
                if len(listed) == 1
                else f"Ignore {len(listed):,} pictures for their items"
            )
        elif scope == VALUE:
            values = _values(listed)
            label = (
                f"Ignore \u201c{values[0]}\u201d in every file"
                if len(values) == 1
                else f"Ignore {len(values):,} values in every file"
            )
        else:
            label = f"Ignore pictures of every {', '.join(self._type_names(listed))}"
        self.ignore_requested.emit(listed, scope, label)

    def _stop_ignoring_picture(self) -> None:
        problem = self.selected_picture()
        if problem is not None and problem.ignored is not None:
            self.stop_ignoring_requested.emit(problem, f"Stop ignoring {problem.title}'s picture")

    def _open_picture_item(self) -> None:
        problem = self.selected_picture()
        if problem is not None:
            self.open_requested.emit(problem.entity_id)

    def _reread_picture_item(self) -> None:
        problem = self.selected_picture()
        if problem is not None:
            self.reread_requested.emit([problem.entity_id])

    def _show_picture_file(self) -> None:
        problem = self.selected_picture()
        if problem is None or self.file_opener is None:
            return
        base = _known(self.session)(problem.root_id)
        if base is None:
            self.message.emit("Its watched folder has no folder on this computer.")
            return
        self.file_opener.act(
            FileToOpen(problem.resource_id, local_path(base, problem.relpath), False), REVEAL
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
        self.missing.selected_entity_ids(self._delete_items)

    def _delete_items(self, ids: list[int]) -> None:
        if ids:
            self.delete_requested.emit(ids)

    def _show_where(self) -> None:
        self.missing.selected_entity_ids(self._show_where_of)

    def _show_where_of(self, ids: list[int]) -> None:
        """Open the folder the first item's first file was in."""
        if not ids:
            return
        session = self.session

        def job() -> str:
            with session.reader.connect() as conn:
                files = files_to_open(conn, session.schema, ids[0], _known(session))
            paths = [f.path for f in files if f.path]
            if not paths:
                return ""
            folder = os.path.dirname(paths[0])
            return folder if os.path.isdir(folder) else f"!{folder}"

        run_in_pool(job, on_done=self._found_folder)

    def _found_folder(self, folder: str) -> None:
        if not shiboken6.isValid(self):
            return
        if not folder:
            self.message.emit("Its root has no folder on this computer.")
        elif folder.startswith("!"):
            self.message.emit(f"Its folder is gone too: {folder[1:]}")
        elif self.file_opener is not None:
            self.file_opener.act(FileToOpen(0, folder, True), OPEN)


def _values(problems: list[PictureProblem]) -> list[str]:
    """The values these not-found rows name, each once (case aside), as first written."""
    found: dict[str, str] = {}
    for p in problems:
        if p.kind == NOT_FOUND and p.written:
            found.setdefault(p.written.strip().casefold(), p.written.strip())
    return list(found.values())


def _markdown_rows(conn: Connection) -> list[MarkdownRow]:
    """The Markdown tab's rows, by path: notes first where a file is both."""
    rows: list[MarkdownRow] = [*note_problems(conn), *markdown_problems(conn)]
    return sorted(rows, key=lambda r: (r.relpath.casefold(), isinstance(r, MarkdownFileProblem)))


def _known(session: KeepSession) -> Callable[[str], str | None]:
    def path(root_id: str) -> str | None:
        try:
            return session.root_path(root_id)
        except StopIteration:
            return None

    return path


def _tab(main: QWidget, buttons: list[QWidget], note: QLabel | None = None) -> QWidget:
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
