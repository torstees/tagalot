"""Choose what to read inside… (#519, DESIGN.md §12 *Archives*): an archive's contents as
a tree of folders and files, each with a ✕ that hides it (a folder's hides everything under
it), and kind chips that hide or show every file of a type at once.

Hidden rows stay listed, greyed and struck through, like a kind chip that's off; clicking ✕
again shows them. **Show hidden files** tucks them away in a long list. The archive's
listing is read in a worker. Files the keep's theme doesn't read aren't listed (the dialog
says which kinds were left out), nor are folders' own entries, archives inside the archive,
and hidden files, as scans leave them out (#391).

The dialog only gathers the choice: :meth:`ChooseInsideDialog.shown` and
:meth:`ChooseInsideDialog.hidden` are read on OK, and saved by the window's
:class:`~tagalot.ui.archive_actions.ArchiveActions`.
"""

from collections import Counter
from collections.abc import Collection, Iterator

import shiboken6
from PySide6.QtCore import QModelIndex, Qt, QThreadPool, Signal
from PySide6.QtGui import QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.archive_choices import ArchiveChoices
from tagalot.core.archive_members import MemberEntry, list_members
from tagalot.core.formats import format_bytes
from tagalot.core.kinds import NO_EXTENSION
from tagalot.ui.kind_chips import KindChips, kind_label
from tagalot.ui.workers import run_in_pool

HIDE_MARK = chr(0x00D7)  # MULTIPLICATION SIGN, as on the filter bar's chips
"""The mark on each row that hides it."""
NAME, SIZE, HIDE = 0, 1, 2
PATH = Qt.ItemDataRole.UserRole + 1
"""A file row's path inside the archive (folders have none)."""


def left_out_message(left_out: Collection[str]) -> str:
    """What was left out, by kind: ``3 files Tagalot doesn't read here: .txt, .nfo``."""
    if not left_out:
        return ""
    kinds = sorted(
        {"no extension" if (k := _kind(name)) == NO_EXTENSION else k for name in left_out}
    )
    count = len(left_out)
    files = f"{count:,} file{'' if count == 1 else 's'}"
    return f"{files} Tagalot doesn't read here: {', '.join(kinds)}"


def _kind(name: str) -> str:
    """A file's kind for the chips: its extension, lowercased (``.ttf``)."""
    base = name.rpartition("/")[2]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else NO_EXTENSION


class ChooseInsideDialog(QDialog):
    """Choose which files inside one archive are read."""

    loaded = Signal()
    """The listing was read (or couldn't be): the tree is filled."""

    def __init__(
        self,
        current: ArchiveChoices,
        archive_path: str,
        extensions: Collection[str] | None,
        pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("choose_inside")
        self.current = current
        self.setWindowTitle(f"Choose what to read inside {current.name}")
        self.resize(560, 520)
        self._extensions = None if extensions is None else {e.lower() for e in extensions}
        self.files: dict[str, MemberEntry] = {}
        """The files listed, by path inside the archive."""
        self.left_out: list[str] = []
        """Files the theme doesn't read here, not listed."""
        self._hidden: set[str] = set()
        self.error: str | None = None

        self.title = QLabel(f"Reading inside <b>{current.name}</b>…")
        self.title.setObjectName("archive_name")
        self.title.setTextFormat(Qt.TextFormat.RichText)
        self.title.setWordWrap(True)
        self.chips = KindChips(self)
        self.chips.changed.connect(self._kinds_changed)
        self.model = QStandardItemModel(0, 3, self)
        self.model.setHorizontalHeaderLabels(["Name", "Size", ""])
        self.tree = QTreeView()
        self.tree.setObjectName("inside_tree")
        self.tree.setModel(self.model)
        self.tree.setUniformRowHeights(True)
        self.tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(SIZE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(HIDE, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.clicked.connect(self._clicked)
        self.show_hidden = QCheckBox("Show hidden files")
        self.show_hidden.setObjectName("show_hidden")
        self.show_hidden.setChecked(True)
        self.show_hidden.setToolTip("Off: tuck hidden files away, to see what will be read")
        self.show_hidden.toggled.connect(lambda _: self._restyle())
        self.summary = QLabel()
        self.summary.setObjectName("summary")
        self.summary.setWordWrap(True)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Read these")
        self.ok.setToolTip(
            "Save the choice and read the shown files in the background. Hidden files' "
            "items hide, keeping their tags."
        )
        self.ok.setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.chips)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.show_hidden)
        layout.addWidget(self.summary)
        layout.addWidget(self.buttons)

        run_in_pool(
            lambda: list_members(archive_path),
            on_done=self._listed,
            on_error=self._failed,
            pool=pool,
        )

    # --- the choice ---

    def hidden(self) -> set[str]:
        """The files to leave out (paths inside the archive)."""
        return set(self._hidden)

    def shown(self) -> set[str]:
        """The files to read."""
        return set(self.files) - self._hidden

    def set_hidden(self, paths: Collection[str], hidden: bool) -> None:
        """Hide or show these files."""
        wanted = set(paths) & set(self.files)
        if hidden:
            self._hidden |= wanted
        else:
            self._hidden -= wanted
        self._restyle()

    # --- loading ---

    def _listed(self, entries: list[MemberEntry]) -> None:
        if not shiboken6.isValid(self):
            return
        for entry in entries:
            if self._extensions is None or entry.ext in self._extensions:
                self.files[entry.name] = entry
            else:
                self.left_out.append(entry.name)
        self._hidden = self.current.hidden() & set(self.files)
        self._build()
        note = left_out_message(self.left_out)
        self.title.setText(f"Reading inside <b>{self.current.name}</b>")
        self.title.setToolTip(note or "Every file in it is one Tagalot reads here")
        self.ok.setEnabled(bool(self.files))
        if not self.files:
            self.summary.setText(
                "Nothing inside is a file Tagalot reads here." + (f" {note}." if note else "")
            )
        self.loaded.emit()

    def _failed(self, error: BaseException) -> None:
        if not shiboken6.isValid(self):
            return
        self.error = str(error) or type(error).__name__
        self.title.setText(f"Couldn't read inside <b>{self.current.name}</b>: {self.error}")
        self.loaded.emit()

    def _build(self) -> None:
        folders: dict[str, QStandardItem] = {}

        def folder(path: str) -> QStandardItem:
            if not path:
                return self.model.invisibleRootItem()
            found = folders.get(path)
            if found is None:
                parent, _, name = path.rpartition("/")
                found = QStandardItem(name)
                folder(parent).appendRow(self._row(found, ""))
                folders[path] = found
            return found

        for path in sorted(self.files, key=lambda p: (p.count("/") == 0, p.casefold())):
            parent, _, name = path.rpartition("/")
            item = QStandardItem(name)
            item.setData(path, PATH)
            item.setToolTip(path)
            folder(parent).appendRow(self._row(item, format_bytes(self.files[path].size)))
        for path, item in folders.items():
            count = sum(1 for p in self.files if p.startswith(path + "/"))
            item.setText(f"{item.text()}  ({count:,})")
            item.setToolTip(f"{path}: {count:,} file{'' if count == 1 else 's'}")
        self.tree.expandToDepth(0)
        self._restyle()

    def _row(self, item: QStandardItem, size: str) -> list[QStandardItem]:
        mark = QStandardItem(HIDE_MARK)
        mark.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return [item, QStandardItem(size), mark]

    # --- hiding ---

    def _clicked(self, index: QModelIndex) -> None:
        if index.column() == HIDE:
            self.toggle(index.siblingAtColumn(NAME))

    def toggle(self, index: QModelIndex) -> None:
        """What ✕ does on a row: a file hides or shows; a folder hides everything under
        it, or shows it all when it's all hidden."""
        paths = self._paths_under(self.model.itemFromIndex(index))
        if not paths:
            return
        self.set_hidden(paths, not paths <= self._hidden)

    def _paths_under(self, item: QStandardItem) -> set[str]:
        path = item.data(PATH)
        if isinstance(path, str):
            return {path}
        found: set[str] = set()
        for row in range(item.rowCount()):
            child = item.child(row, NAME)
            if child is not None:
                found |= self._paths_under(child)
        return found

    def _kinds_changed(self, hidden_kinds: list[str]) -> None:
        kinds = set(hidden_kinds)
        for kind in {_kind(p) for p in self.files}:
            paths = {p for p in self.files if _kind(p) == kind}
            if kind in kinds:
                self._hidden |= paths
            elif paths <= self._hidden:  # turned on: show them all
                self._hidden -= paths
        self._restyle()

    def _restyle(self) -> None:
        dim = self.palette().color(QPalette.ColorRole.PlaceholderText)
        plain = self.palette().color(QPalette.ColorRole.Text)
        hiding = not self.show_hidden.isChecked()
        for item in self._items(self.model.invisibleRootItem()):
            paths = self._paths_under(item)
            hidden = bool(paths) and paths <= self._hidden
            parent = item.parent() or self.model.invisibleRootItem()
            cells = [parent.child(item.row(), c) for c in (NAME, SIZE, HIDE)]
            for cell in cells:
                font = cell.font()
                font.setStrikeOut(hidden and cell is not cells[HIDE])
                cell.setFont(font)
                cell.setForeground(dim if hidden else plain)
            file = isinstance(item.data(PATH), str)
            if hidden:
                tip = "Show it again" if file else "Show everything in it again"
            else:
                tip = "Hide it: don't read it" if file else "Hide everything in it"
            cells[HIDE].setToolTip(tip)
            self.tree.setRowHidden(item.row(), parent.index(), hiding and hidden)
        self._update_chips()
        shown, total = len(self.files) - len(self._hidden), len(self.files)
        if total:
            note = left_out_message(self.left_out)
            self.summary.setText(
                f"{shown:,} of {total:,} file{'' if total == 1 else 's'} will be read."
                + (f" {note}." if note else "")
            )

    def _items(self, parent: QStandardItem) -> Iterator[QStandardItem]:
        for row in range(parent.rowCount()):
            child = parent.child(row, NAME)
            if child is not None:
                yield child
                yield from self._items(child)

    def _update_chips(self) -> None:
        counts = Counter(_kind(p) for p in self.files)
        labels = {k: kind_label(k, {}) for k in counts}
        tips = {
            k: (f"Hide every {labels[k]} file", f"Show every {labels[k]} file again")
            for k in counts
        }
        self.chips.set_kinds(counts, labels, tips)
        hidden = [k for k in counts if {p for p in self.files if _kind(p) == k} <= self._hidden]
        self.chips.set_hidden(hidden)
        self.chips.setVisible(len(counts) > 1 or bool(hidden))
