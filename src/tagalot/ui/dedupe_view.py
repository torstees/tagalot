"""The Dedupe page: exact duplicate files (DESIGN.md §12 "Dedupe", §13; #117).

TOOLS → Dedupe lists groups of files with the same fingerprint, most space wasted first,
each expanding to its copies: where each is, its size, and the items using it. **Check**
reads the selected groups' files whole and compares them (``core.dedupe.Verifier``, in a
worker), marking each copy *same* or *differs* (or why it couldn't be read); **Check all**
does every group. A copy's menu opens it or shows it in the file manager; double-clicking
a copy opens the item using it.

A second tab, **Similar items**, lists pairs of items the theme finds alike (#118: the
same song in two places, a resized picture), most alike first, with how alike; double-click
opens the first item, and the menu either one.

Under both lists, the **compare pane** (#119, ``ui.compare_view``) shows the current
group's items, or the current pair, side by side, highlighting what differs.

Its **Merge…** opens ``ui.merge_dialog`` (#120); **Keep both as versions** merges into
the left item at once (#121). **Not a duplicate** hides the current group or pair
(``core.not_duplicates``; a group shows again once its files change), and **Show
dismissed** lists those greyed, to show them as duplicates again. **Link as one** (#600)
makes the current group's copies places of one file (``core.place_links.link_as_one``),
for links a share shows as ordinary files; not once **Check** found them different.

A third tab, **Linked folders** (#601, ``core.linked_folders``), suggests folders with the
same name whose files match, for linked folders a share shows as ordinary ones. The
selected pair's preview lists the files that match, those that differ, and those on one
side only. **Link folders** links them as one; files that differ block it, with a
warning, until **Check again** (reading them whole) finds them the same. **Not the same
folder** hides a pair, as Not a duplicate does.
"""

from functools import partial

import shiboken6
from PySide6.QtCore import QModelIndex, QPoint, Qt, Signal
from PySide6.QtGui import QPalette, QShowEvent, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.dedupe import (
    DuplicateFile,
    DuplicateGroup,
    NearDuplicates,
    NearPair,
    Verification,
    exact_groups,
    near_duplicates,
)
from tagalot.core.formats import format_bytes
from tagalot.core.handlers import OPEN, REVEAL
from tagalot.core.linked_folders import FolderPair, PairedPath, confirm_same, suggest_linked_folders
from tagalot.core.not_duplicates import (
    EXACT,
    Dismissals,
    Entry,
    folder_entry,
    group_entry,
    load_dismissals,
    pair_entry,
)
from tagalot.core.search_fields import type_labels
from tagalot.core.session import KeepSession
from tagalot.ui.archive_actions import ArchiveActions
from tagalot.ui.compare_view import ComparePane, known_root
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.thumbnails import ThumbnailLoader
from tagalot.ui.workers import run_in_pool

COLUMNS = ("Name", "Where", "Size", "Items", "Check")
SIMILAR_COLUMNS = ("Alike", "Type", "Item", "And")
FOLDER_COLUMNS = ("Folder", "And", "Match", "Differ", "One side only")
FOLDERS_TAB = 2
MAX_PREVIEW = 500
"""Paths a preview section lists at most (its count says how many there are)."""
NAME, WHERE, SIZE, ITEMS, CHECK = range(5)
_GROUP = Qt.ItemDataRole.UserRole
_FILE = Qt.ItemDataRole.UserRole + 1


class DedupePage(QWidget):
    """See the module docstring."""

    open_entity = Signal(int)
    merge_requested = Signal(list)
    """The compare pane's Merge…: (id, title, where) of each item."""
    versions_requested = Signal(list)
    """The compare pane's Keep both as versions: the item ids, the one to keep first."""
    not_duplicate_requested = Signal(list, bool)
    """Not a duplicate (``True``) or Show as a duplicate again (``False``): the entries."""
    link_requested = Signal(list)
    """Link as one (#600): the current group's copies, the first place first."""
    link_folders_requested = Signal(object, object, object)
    """Link folders (#601): the first folder, the other, and the ``(first id, other id)``
    pairs that differed but were found the same on a full read."""

    def __init__(
        self,
        session: KeepSession,
        file_opener: FileOpener | None = None,
        parent: QWidget | None = None,
        *,
        thumbnails: ThumbnailLoader | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.file_opener = file_opener
        self.archive_actions: ArchiveActions | None = None
        """Hide inside its archive, for a copy inside one (#519): set by the window."""
        self.groups: list[DuplicateGroup] | None = None
        """The groups listed (dismissed ones only with Show dismissed)."""
        self.all_groups: list[DuplicateGroup] = []
        self.all_pairs = NearDuplicates([])
        self.dismissals = Dismissals()
        self._hidden_groups = self._hidden_pairs = self._hidden_folders = 0
        self.all_folders: list[FolderPair] = []
        self.folder_pairs: list[FolderPair] | None = None
        """The pairs of folders listed (dismissed ones only with Show dismissed)."""
        self.folder_checks: dict[Entry, set[tuple[int, int]]] = {}
        """Check again's results, by pair: the files that differed but read the same."""
        self.checking_folders = 0
        self.checks: dict[bytes, Verification] = {}
        self.checking = 0
        self._generation = 0

        self.summary = QLabel()
        self.summary.setObjectName("summary")
        self.check_button = QPushButton("Check")
        self.check_button.setToolTip(
            "Read the selected groups' files whole and compare them (slow on a share)"
        )
        self.check_button.clicked.connect(lambda: self.check(self.selected_groups()))
        self.check_all_button = QPushButton("Check all")
        self.check_all_button.clicked.connect(lambda: self.check(self.groups or []))
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        header = QHBoxLayout()
        header.addWidget(self.summary, 1)
        header.addWidget(self.check_button)
        header.addWidget(self.check_all_button)
        header.addWidget(refresh)

        self.model = QStandardItemModel(0, len(COLUMNS))
        self.model.setHorizontalHeaderLabels(list(COLUMNS))
        self.tree = QTreeView()
        self.tree.setObjectName("groups")
        self.tree.setModel(self.model)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.doubleClicked.connect(self._activated)
        self.tree.selectionModel().selectionChanged.connect(lambda *_: self._enable_check())
        self.tree.header().setSectionResizeMode(WHERE, QHeaderView.ResizeMode.Stretch)

        identical = QWidget()
        column = QVBoxLayout(identical)
        column.addLayout(header)
        column.addWidget(self.tree, 1)

        self.similar_summary = QLabel()
        self.similar_summary.setObjectName("similar_summary")
        self.similar_summary.setWordWrap(True)
        self.pairs: NearDuplicates | None = None
        self.similar = QStandardItemModel(0, len(SIMILAR_COLUMNS))
        self.similar.setHorizontalHeaderLabels(list(SIMILAR_COLUMNS))
        self.similar_table = QTableView()
        self.similar_table.setObjectName("similar")
        self.similar_table.setModel(self.similar)
        self.similar_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.similar_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.similar_table.verticalHeader().hide()
        self.similar_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.similar_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.similar_table.doubleClicked.connect(
            lambda index: self._open_pair(index.row(), first=True)
        )
        self.similar_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.similar_table.customContextMenuRequested.connect(self._pair_menu)
        similar = QWidget()
        column = QVBoxLayout(similar)
        column.addWidget(self.similar_summary)
        column.addWidget(self.similar_table, 1)

        self.tabs = QTabWidget()
        self.tabs.addTab(identical, "Identical files")
        self.tabs.addTab(similar, "Similar items")
        self.tabs.addTab(self._folders_tab(), "Linked folders")
        self.show_dismissed = QCheckBox("Show dismissed")
        self.show_dismissed.setObjectName("show_dismissed")
        self.show_dismissed.setToolTip("List the groups and pairs marked as not duplicates, greyed")
        self.show_dismissed.toggled.connect(lambda _: self._relist())
        self.tabs.setCornerWidget(self.show_dismissed)
        self.compare_pane = ComparePane(session, thumbnails)
        self.compare_pane.open_entity.connect(self.open_entity)
        self.compare_pane.merge_requested.connect(self.merge_requested)
        self.compare_pane.versions_requested.connect(self.versions_requested)
        self.not_duplicate_button = QPushButton("Not a duplicate")
        self.not_duplicate_button.setObjectName("not_duplicate")
        self.not_duplicate_button.clicked.connect(self._not_duplicate)
        self.not_duplicate_button.setVisible(False)
        self.compare_pane.add_action(self.not_duplicate_button)
        self.link_button = QPushButton("Link as one")
        self.link_button.setObjectName("link_as_one")
        self.link_button.clicked.connect(self._link)
        self.link_button.setVisible(False)
        self.compare_pane.add_action(self.link_button)
        self.tree.selectionModel().currentRowChanged.connect(lambda *_: self._compare())
        self.similar_table.selectionModel().currentRowChanged.connect(lambda *_: self._compare())
        self.folders_table.selectionModel().currentRowChanged.connect(lambda *_: self._compare())
        self.tabs.currentChanged.connect(lambda _: self._compare())
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.tabs)
        self.splitter.addWidget(self.compare_pane)
        self._split = False
        layout = QVBoxLayout(self)
        layout.addWidget(self.splitter)
        self.refresh()

    def showEvent(self, event: QShowEvent) -> None:
        """The first time it's shown, the compare pane gets the larger share."""
        super().showEvent(event)
        if not self._split:
            self._split = True
            height = self.splitter.height()
            self.splitter.setSizes([height * 2 // 5, height - height * 2 // 5])

    # --- reading ---

    def refresh(self) -> None:
        """Find the duplicate groups again, in a worker (checks already done are kept)."""
        self._generation += 1
        generation, session = self._generation, self.session
        self.summary.setText("Looking for duplicates\u2026")

        def job() -> tuple[list[DuplicateGroup], Dismissals]:
            with session.reader.connect() as conn:
                return exact_groups(conn, known_root(session)), load_dismissals(conn)

        def done(found: tuple[list[DuplicateGroup], Dismissals]) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self.all_groups, self.dismissals = found
                self._show(self.all_groups)

        run_in_pool(job, on_done=done)
        self.similar_summary.setText("Looking for similar items\u2026")

        def similar_job() -> tuple[NearDuplicates, Dismissals]:
            with session.reader.connect() as conn:
                return near_duplicates(conn, session.schema), load_dismissals(conn)

        def similar_done(found: tuple[NearDuplicates, Dismissals]) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self.all_pairs, self.dismissals = found
                self._show_pairs(self.all_pairs)

        run_in_pool(similar_job, on_done=similar_done)
        self.folders_summary.setText("Looking for linked folders\u2026")

        def folders_job() -> tuple[list[FolderPair], Dismissals]:
            with session.reader.connect() as conn:
                return suggest_linked_folders(conn), load_dismissals(conn)

        def folders_done(found: tuple[list[FolderPair], Dismissals]) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self.all_folders, self.dismissals = found
                self._show_folders(self.all_folders)

        run_in_pool(folders_job, on_done=folders_done)

    # --- similar items ---

    def _show_pairs(self, everything: NearDuplicates) -> None:
        current = self._current_pair()
        hidden = [p for p in everything.pairs if self.dismissals.hides(pair_entry(p))]
        shown = (
            everything.pairs
            if self.show_dismissed.isChecked()
            else [p for p in everything.pairs if p not in hidden]
        )
        found = NearDuplicates(shown, everything.skipped_keys)
        self.pairs = found
        self.similar.removeRows(0, self.similar.rowCount())
        labels = type_labels(self.session.schema)
        for pair in found.pairs:
            cells = [
                QStandardItem(f"{round(pair.score * 100)}%"),
                QStandardItem(labels.get(pair.type_id, pair.type_id)),
                QStandardItem(pair.a[1]),
                QStandardItem(pair.b[1]),
            ]
            if pair in hidden:
                _grey(cells)
            self.similar.appendRow(cells)
        n = len(everything.pairs) - len(hidden)
        text = (
            f"{n:,} {'pair' if n == 1 else 'pairs'} of items that look alike, most alike first."
            if n
            else "No similar items found."
        )
        if hidden:
            text += f" {len(hidden):,} marked as not duplicates."
        self._hidden_pairs = len(hidden)
        self._count_dismissed()
        if found.skipped_keys:
            text += (
                f" {found.skipped_keys:,} groups too large to compare were skipped "
                "(the theme's keys are too loose for them)."
            )
        self.similar_summary.setText(text)
        self.tabs.setTabText(1, f"Similar items ({n:,})" if n else "Similar items")
        if current is not None:  # the same pair stays current (after tagging one, say)
            ids = (current.a[0], current.b[0])  # (gone when it was just dismissed)
            row = next((i for i, p in enumerate(found.pairs) if (p.a[0], p.b[0]) == ids), None)
            if row is not None:
                self.similar_table.setCurrentIndex(self.similar.index(row, 0))
        if self.tabs.currentIndex() == 1:
            self._compare()
        self.similar_table.resizeColumnToContents(0)
        self.similar_table.resizeColumnToContents(1)

    # --- comparing ---

    def _compare(self) -> None:
        """Show the current tab's current group or pair in the compare pane."""
        pane = self.compare_pane
        entry = self._current_entry()
        self.not_duplicate_button.setVisible(entry is not None)
        self._show_link()
        if entry is not None:
            dismissed = self.dismissals.hides(entry)
            self.not_duplicate_button.setText(
                "Show as a duplicate again" if dismissed else "Not a duplicate"
            )
            self.not_duplicate_button.setToolTip(
                "List it with the others again"
                if dismissed
                else "Hide it from this page (Show dismissed lists it again)"
                + ("; it comes back if its copies change" if entry[0] == EXACT else "")
            )
        if self.tabs.currentIndex() == FOLDERS_TAB:
            self.not_duplicate_button.setText(
                "Show as a suggestion again"
                if entry is not None and self.dismissals.hides(entry)
                else "Not the same folder"
            )
            self._preview_folders()
            folders = self._current_folders()
            pane.clear(
                "Select a pair of folders to preview it above."
                if folders is None
                else "Linking makes each file that matches one item, listed in both folders."
            )
            return
        if self.tabs.currentIndex() == 1:
            pair = self._current_pair()
            if pair is None:
                pane.clear("Select a pair to compare its items here.")
                return
            pane.compare([pair.a[0], pair.b[0]], f"{round(pair.score * 100)}% alike.")
            return
        group = self._current_group()
        if group is None:
            pane.clear("Select a group to compare the items using its copies here.")
            return
        items = dict.fromkeys(i for file in group.files for i in file.items)
        if not items:
            pane.clear("No item uses these files.")
        elif len(items) == 1:
            [(entity_id, title)] = items
            pane.compare([entity_id], f"Every copy belongs to one item, {title}.")
        else:
            pane.compare([i for i, _ in items], f"{len(items):,} items use these copies.")

    def _current_entry(self) -> Entry | None:
        """How the current group or pair would be dismissed, if there is one."""
        if self.tabs.currentIndex() == FOLDERS_TAB:
            folders = self._current_folders()
            return folder_entry(folders) if folders is not None else None
        if self.tabs.currentIndex() == 1:
            pair = self._current_pair()
            return pair_entry(pair) if pair is not None else None
        group = self._current_group()
        return group_entry(group) if group is not None else None

    def _show_link(self) -> None:
        """Link as one, for the current group of identical files: not once its copies were
        found to differ."""
        group = self._current_group() if self.tabs.currentIndex() == 0 else None
        self.link_button.setVisible(group is not None)
        if group is None:
            return
        check = self.checks.get(group.fingerprint)
        differs = check is not None and bool(check.different)
        self.link_button.setEnabled(not differs)
        self.link_button.setToolTip(
            "Check found these copies different, so they aren't one file"
            if differs
            else "These are one file in several places (links your share shows as files): "
            "make them one item, listed in each place (Edit \u2192 Undo)"
        )

    def _link(self) -> None:
        group = self._current_group()
        if group is not None:
            self.link_requested.emit([f.resource_id for f in group.files])

    def _not_duplicate(self) -> None:
        entry = self._current_entry()
        if entry is not None:
            self.not_duplicate_requested.emit([entry], not self.dismissals.hides(entry))

    def _relist(self) -> None:
        self._show(self.all_groups)
        self._show_pairs(self.all_pairs)
        self._show_folders(self.all_folders)

    def _count_dismissed(self) -> None:
        n = self._hidden_groups + self._hidden_pairs + self._hidden_folders
        self.show_dismissed.setText(f"Show dismissed ({n:,})" if n else "Show dismissed")
        self.show_dismissed.setEnabled(bool(n) or self.show_dismissed.isChecked())

    # --- linked folders ---

    def _folders_tab(self) -> QWidget:
        self.folders_summary = QLabel()
        self.folders_summary.setObjectName("folders_summary")
        self.folders_summary.setWordWrap(True)
        self.folders = QStandardItemModel(0, len(FOLDER_COLUMNS))
        self.folders.setHorizontalHeaderLabels(list(FOLDER_COLUMNS))
        self.folders_table = QTableView()
        self.folders_table.setObjectName("linked_folders")
        self.folders_table.setModel(self.folders)
        self.folders_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.folders_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.folders_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.folders_table.verticalHeader().hide()
        header = self.folders_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.preview = QStandardItemModel(0, 2)
        self.preview.setHorizontalHeaderLabels(["Inside the folders", "Check"])
        self.preview_tree = QTreeView()
        self.preview_tree.setObjectName("folder_preview")
        self.preview_tree.setModel(self.preview)
        self.preview_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.folder_warning = QLabel()
        self.folder_warning.setObjectName("folder_warning")
        self.folder_warning.setWordWrap(True)
        self.link_folders_button = QPushButton("Link folders")
        self.link_folders_button.setObjectName("link_folders")
        self.link_folders_button.clicked.connect(self._link_folders)
        self.check_folders_button = QPushButton("Check again")
        self.check_folders_button.setObjectName("check_folders")
        self.check_folders_button.setToolTip(
            "Read the files that differ whole, in both folders, and compare them (slow on a share)"
        )
        self.check_folders_button.clicked.connect(self._check_folders)
        buttons = QHBoxLayout()
        buttons.addWidget(self.folder_warning, 1)
        buttons.addWidget(self.check_folders_button)
        buttons.addWidget(self.link_folders_button)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(self.folders_table)
        split.addWidget(self.preview_tree)
        tab = QWidget()
        column = QVBoxLayout(tab)
        column.addWidget(self.folders_summary)
        column.addWidget(split, 1)
        column.addLayout(buttons)
        self._preview_folders()
        return tab

    def _show_folders(self, everything: list[FolderPair]) -> None:
        current = self._current_folders()
        hidden = [p for p in everything if self.dismissals.hides(folder_entry(p))]
        shown = (
            everything
            if self.show_dismissed.isChecked()
            else [p for p in everything if p not in hidden]
        )
        self.folder_pairs = shown
        self.folders.removeRows(0, self.folders.rowCount())
        for pair in shown:
            one_side = len(pair.only_first) + len(pair.only_other)
            cells = [
                QStandardItem(_where(pair.first.root_name, pair.first.relpath)),
                QStandardItem(_where(pair.other.root_name, pair.other.relpath)),
                QStandardItem(f"{pair.files_same:,}"),
                QStandardItem(f"{len(pair.differ):,}" if pair.differ else ""),
                QStandardItem(f"{one_side:,}" if one_side else ""),
            ]
            if pair in hidden:
                _grey(cells)
            self.folders.appendRow(cells)
        n = len(everything) - len(hidden)
        text = (
            f"{n:,} {'pair' if n == 1 else 'pairs'} of folders with the same name and "
            "matching files. If one is a link to the other (your share shows links as "
            "ordinary folders), link them: each file becomes one item, listed in both."
            if n
            else "No folders look linked."
        )
        if hidden:
            text += f" {len(hidden):,} marked as not the same folder."
        self.folders_summary.setText(text)
        self._hidden_folders = len(hidden)
        self._count_dismissed()
        self.tabs.setTabText(FOLDERS_TAB, f"Linked folders ({n:,})" if n else "Linked folders")
        if current is not None:  # the same pair stays current
            key = folder_entry(current)
            row = next((i for i, p in enumerate(shown) if folder_entry(p) == key), None)
            if row is not None:
                self.folders_table.setCurrentIndex(self.folders.index(row, 0))
        self.folders_table.resizeColumnsToContents()
        if self.tabs.currentIndex() == FOLDERS_TAB:
            self._compare()
        else:
            self._preview_folders()

    def _current_folders(self) -> FolderPair | None:
        row = self.folders_table.currentIndex().row()
        if self.folder_pairs is None or not 0 <= row < len(self.folder_pairs):
            return None
        return self.folder_pairs[row]

    def _preview_folders(self) -> None:
        """The current pair's preview, the warning, and what the buttons can do."""
        self.preview.removeRows(0, self.preview.rowCount())
        pair = self._current_folders()
        self.link_folders_button.setEnabled(False)
        self.check_folders_button.setVisible(False)
        if pair is None:
            self.folder_warning.setText("")
            self.link_folders_button.setToolTip("Select a pair of folders to link them")
            return
        same_on_reading = self.folder_checks.get(folder_entry(pair))
        checked = same_on_reading or set()
        blocking = [p for p in pair.differ if (p.first_id, p.other_id) not in checked]

        def section(title: str, paths: list[str], checks: list[str] | None = None) -> None:
            head = QStandardItem(f"{title} ({len(paths):,})")
            for n, path in enumerate(paths[:MAX_PREVIEW]):
                head.appendRow([QStandardItem(path), QStandardItem(checks[n] if checks else "")])
            if len(paths) > MAX_PREVIEW:
                head.appendRow(QStandardItem(f"\u2026 and {len(paths) - MAX_PREVIEW:,} more"))
            self.preview.appendRow([head, QStandardItem("")])

        if pair.differ:
            section(
                "Differ",
                [p.inside or "(the folders)" for p in pair.differ],
                [
                    (
                        "same, read whole"
                        if (p.first_id, p.other_id) in checked
                        else "differs"
                        if same_on_reading is not None
                        else ""
                    )
                    for p in pair.differ
                ],
            )
        section(
            "Match",
            [p.inside + ("  (linked already)" if p.linked else "") for p in pair.same if p.inside],
        )
        if pair.only_first:
            section(f"Only in {pair.first.shown}", list(pair.only_first))
        if pair.only_other:
            section(f"Only in {pair.other.shown}", list(pair.only_other))
        if pair.differ:
            self.preview_tree.expand(self.preview.index(0, 0))
        self.preview_tree.resizeColumnToContents(1)
        self.check_folders_button.setVisible(bool(pair.differ))
        self.check_folders_button.setEnabled(bool(blocking) and not self.checking_folders)
        if blocking:
            n = len(blocking)
            self.folder_warning.setText(
                f"\u26a0 {n:,} {'file differs' if n == 1 else 'files differ'}, so these "
                "may be copies (one edited) rather than one folder: two places of one file "
                "can't differ. A file changing while it was read, or a network glitch, can "
                "make one look different: Check again reads them whole."
            )
            self.link_folders_button.setToolTip("Files that differ block linking")
            return
        self.folder_warning.setText(
            "The files that differed are the same, read whole." if pair.differ else ""
        )
        self.link_folders_button.setEnabled(True)
        self.link_folders_button.setToolTip(
            "These are one folder in two places: make each file that matches one item, "
            "listed in both, and link files that turn up in both later (Edit \u2192 Undo)"
        )

    def _check_folders(self) -> None:
        """Check again: read the current pair's differing files whole, in a worker."""
        pair = self._current_folders()
        if pair is None or not pair.differ:
            return
        session, key = self.session, folder_entry(pair)
        differ: tuple[PairedPath, ...] = pair.differ
        self.checking_folders += 1
        self.folder_warning.setText("Reading the files that differ\u2026")
        self.check_folders_button.setEnabled(False)

        def job() -> set[tuple[int, int]]:
            with session.reader.connect() as conn:
                return confirm_same(conn, known_root(session), session.verifier, differ)

        def done(found: set[tuple[int, int]]) -> None:
            if not shiboken6.isValid(self):
                return
            self.checking_folders -= 1
            self.folder_checks[key] = found
            self._preview_folders()

        run_in_pool(job, on_done=done)

    def _link_folders(self) -> None:
        pair = self._current_folders()
        if pair is not None:
            checked = self.folder_checks.get(folder_entry(pair), set())
            self.link_folders_requested.emit(pair.first, pair.other, set(checked))

    def _current_pair(self) -> NearPair | None:
        row = self.similar_table.currentIndex().row()
        if self.pairs is None or not 0 <= row < len(self.pairs.pairs):
            return None
        return self.pairs.pairs[row]

    def _current_group(self) -> DuplicateGroup | None:
        index = self.tree.currentIndex()
        if not index.isValid():
            return None
        top = index.parent() if index.parent().isValid() else index
        fingerprint = self.model.item(top.row(), 0).data(_GROUP)
        return next((g for g in self.groups or [] if g.fingerprint == fingerprint), None)

    def _open_pair(self, row: int, first: bool) -> None:
        if self.pairs is not None and 0 <= row < len(self.pairs.pairs):
            pair = self.pairs.pairs[row]
            self.open_entity.emit((pair.a if first else pair.b)[0])

    def _pair_menu(self, point: QPoint) -> None:
        row = self.similar_table.indexAt(point).row()
        if self.pairs is None or not 0 <= row < len(self.pairs.pairs):
            return
        pair = self.pairs.pairs[row]
        menu = QMenu(self)
        menu.addAction(f"Open {pair.a[1]}").triggered.connect(lambda: self._open_pair(row, True))
        menu.addAction(f"Open {pair.b[1]}").triggered.connect(lambda: self._open_pair(row, False))
        menu.exec(self.similar_table.viewport().mapToGlobal(point))

    def _show(self, everything: list[DuplicateGroup]) -> None:
        current = self._current_group()
        hidden = [g for g in everything if self.dismissals.hides(group_entry(g))]
        listed = [g for g in everything if g not in hidden]
        groups = everything if self.show_dismissed.isChecked() else listed
        self.groups = groups
        self.model.removeRows(0, self.model.rowCount())
        for group in groups:
            row = self._group_row(group)
            for file in group.files:
                row[0].appendRow(self._file_row(file))
            if group in hidden:
                _grey(row)
            self.model.appendRow(row)
            self._show_check(group)
        wasted = sum(g.wasted for g in listed)
        text = (
            f"{len(listed):,} {'group' if len(listed) == 1 else 'groups'} of identical "
            "files; the extra copies take "
            f"{format_bytes(wasted)}."
            if listed
            else "No duplicate files found."
        )
        if hidden:
            text += f" {len(hidden):,} marked as not duplicates."
        self.summary.setText(text)
        self._hidden_groups = len(hidden)
        self._count_dismissed()
        self.check_all_button.setEnabled(bool(groups))
        if current is not None:  # the same group stays current
            item = self._row_of(current)
            if item is not None:
                self.tree.setCurrentIndex(item.index())
        self._enable_check()
        if self.tabs.currentIndex() == 0:
            self._compare()
        for column in (NAME, SIZE, ITEMS, CHECK):
            self.tree.resizeColumnToContents(column)

    def _group_row(self, group: DuplicateGroup) -> list[QStandardItem]:
        name = group.files[0].relpath.rpartition("/")[2]
        cells = [
            QStandardItem(f"{name}  \u00d7{len(group.files)}"),
            QStandardItem(f"{format_bytes(group.wasted)} extra"),
            QStandardItem(format_bytes(group.size)),
            QStandardItem(""),
            QStandardItem("not checked"),
        ]
        cells[0].setData(group.fingerprint, _GROUP)
        cells[0].setToolTip(f"{len(group.files)} files with the same fingerprint")
        return cells

    def _file_row(self, file: DuplicateFile) -> list[QStandardItem]:
        folder, _, name = file.shown.rpartition("/")
        where = f"{file.root_name} \u203a {folder}" if folder else file.root_name
        items = ", ".join(title for _, title in file.items) or "(no item)"
        cells = [
            QStandardItem(name),
            QStandardItem(where),
            QStandardItem(format_bytes(file.size)),
            QStandardItem(items),
            QStandardItem("offline" if file.status.value == "offline" else ""),
        ]
        cells[0].setData(file, _FILE)
        if file.path is None:
            cells[1].setToolTip("Its folder has no path on this computer")
        elif file.archive:  # opened from a copy; the archive is never changed (#524)
            cells[1].setToolTip(f"Inside the archive {file.path}")
        else:
            cells[1].setToolTip(file.path)
        return cells

    # --- checking ---

    def selected_groups(self) -> list[DuplicateGroup]:
        """The groups of the selected rows (a copy's row selects its group)."""
        wanted: dict[bytes, None] = {}
        for index in self.tree.selectionModel().selectedRows():
            top = index.parent() if index.parent().isValid() else index
            fingerprint = self.model.item(top.row(), 0).data(_GROUP)
            if isinstance(fingerprint, bytes):
                wanted[fingerprint] = None
        return [g for g in self.groups or [] if g.fingerprint in wanted]

    def _enable_check(self) -> None:
        """Check works on the selection: it's greyed out (and says why) without one."""
        selected = bool(self.tree.selectionModel().selectedRows())
        self.check_button.setEnabled(selected)
        self.check_button.setToolTip(
            "Read the selected groups' files whole and compare them (slow on a share)"
            if selected
            else "Select a group (or one of its copies) to check it, or use Check all"
        )

    def check(self, groups: list[DuplicateGroup]) -> None:
        """Verify these groups by full hash, one worker job each."""
        verifier = self.session.verifier
        for group in groups:
            self.checking += 1
            self._set_check(group, "checking\u2026")

            def done(result: Verification, group: DuplicateGroup = group) -> None:
                if not shiboken6.isValid(self):
                    return
                self.checking -= 1
                self.checks[group.fingerprint] = result
                self._show_check(group)
                self._show_link()  # not once its copies differ

            run_in_pool(partial(verifier.verify_group, group), on_done=done)

    def _show_check(self, group: DuplicateGroup) -> None:
        result = self.checks.get(group.fingerprint)
        if result is None:
            return
        row = self._row_of(group)
        if row is None:
            return
        same = {i for ids in result.identical for i in ids}
        for n in range(row.rowCount()):
            file = row.child(n, 0).data(_FILE)
            if not isinstance(file, DuplicateFile):
                continue
            rid = file.resource_id
            text = (
                "same"
                if rid in same
                else f"couldn't read: {result.unread[rid]}"
                if rid in result.unread
                else "differs"
            )
            if rid in result.changed:
                text += " (changed since the last scan; press F5)"
            row.child(n, CHECK).setText(text)
        if result.confirmed:
            summary = "identical"
        elif result.different or len(result.identical) > 1:
            summary = "not all the same"
        else:
            summary = f"identical where read ({len(result.unread)} not read)"
        self._set_check(group, summary)

    def _set_check(self, group: DuplicateGroup, text: str) -> None:
        row = self._row_of(group)
        if row is not None:
            self.model.item(row.row(), CHECK).setText(text)

    def _row_of(self, group: DuplicateGroup) -> QStandardItem | None:
        for n in range(self.model.rowCount()):
            item = self.model.item(n, 0)
            if item.data(_GROUP) == group.fingerprint:
                return item
        return None

    # --- files ---

    def _file_at(self, index: QModelIndex) -> DuplicateFile | None:
        if not index.isValid() or not index.parent().isValid():
            return None
        file = self.model.itemFromIndex(index.siblingAtColumn(0)).data(_FILE)
        return file if isinstance(file, DuplicateFile) else None

    def _menu(self, point: QPoint) -> None:
        file = self._file_at(self.tree.indexAt(point))
        menu = None if file is None else self.file_menu(file)
        if menu is not None:
            menu.exec(self.tree.viewport().mapToGlobal(point))

    def file_menu(self, file: DuplicateFile) -> QMenu | None:
        """A copy's right-click menu (``None`` without a path here to open it by)."""
        if file.path is None or self.file_opener is None:
            return None
        opener = self.file_opener
        menu = QMenu(self)
        # Looked up by id: a file inside an archive opens from a copy, and Show in file
        # manager shows its archive (#524).
        open_file = menu.addAction("Open file")
        open_file.triggered.connect(lambda: opener.act_on_resource(file.resource_id, OPEN))
        reveal = menu.addAction(
            "Show the archive in file manager" if file.archive else "Show in file manager"
        )
        reveal.triggered.connect(lambda: opener.act_on_resource(file.resource_id, REVEAL))
        if file.archive:
            open_file.setToolTip("Open a copy with its usual program; the archive isn't changed")
            if self.archive_actions is not None:
                # Duplicates never removes a file; a copy inside an archive can be hidden.
                self.archive_actions.add_hide(menu, file.resource_id)
        for entity_id, title in file.items:
            menu.addAction(f"Open {title}").triggered.connect(
                lambda _=False, e=entity_id: self.open_entity.emit(e)
            )
        return menu

    def _activated(self, index: QModelIndex) -> None:
        file = self._file_at(index)
        if file is not None and file.items:
            self.open_entity.emit(file.items[0][0])


def _where(root_name: str, relpath: str) -> str:
    return f"{root_name} \u203a {relpath}"


def _grey(cells: list[QStandardItem]) -> None:
    """A dismissed group's or pair's row: greyed, saying why."""
    for cell in cells:
        cell.setForeground(QPalette().color(QPalette.ColorRole.PlaceholderText))
        cell.setToolTip("Marked as not a duplicate")
