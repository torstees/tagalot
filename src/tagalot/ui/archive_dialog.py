"""Choose what to read inside… (#519, DESIGN.md §12 *Archives*): an archive's contents as
a tree of folders and files, each with a ✕ that hides it (a folder's hides everything under
it), and kind chips that hide or show every file of a type at once.

Hidden rows stay listed, greyed and struck through, like a kind chip that's off; clicking ✕
again shows them. **Show hidden files** tucks them away in a long list. **Show all** and
**Hide all** start from everything or nothing, whichever leaves fewer clicks. **Show
thumbnails** swaps the tree for a grid of the files' thumbnails (pictures, fonts, PDFs...),
drawn a few at a time in a worker and cached like a gallery's archive pictures; clicking a
tile hides or shows it. The archive's listing is read in a worker. Files
the keep's theme doesn't read aren't listed (the dialog says which kinds were left out), nor
are folders' own entries, archives inside the archive, and hidden files, as scans leave them
out (#391).

The dialog only gathers the choice: :meth:`ChooseInsideDialog.shown` and
:meth:`ChooseInsideDialog.hidden` are read on OK, and saved by the window's
:class:`~tagalot.ui.archive_actions.ArchiveActions`.
"""

from collections import Counter
from collections.abc import Collection, Iterator

import shiboken6
from PySide6.QtCore import QModelIndex, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import (
    QIcon,
    QImage,
    QPainter,
    QPalette,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListView,
    QPushButton,
    QStackedWidget,
    QStyle,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.archive_choices import ArchiveChoices
from tagalot.core.archive_members import MemberEntry, list_members
from tagalot.core.formats import format_bytes
from tagalot.core.kinds import NO_EXTENSION
from tagalot.core.thumbnails.resolve import ThumbnailResolver, member_renderer
from tagalot.ui.kind_chips import KindChips, kind_label
from tagalot.ui.workers import run_in_pool

HIDE_MARK = chr(0x00D7)  # MULTIPLICATION SIGN, as on the filter bar's chips
"""The mark on each row that hides it."""
NAME, SIZE, HIDE = 0, 1, 2
PATH = Qt.ItemDataRole.UserRole + 1
"""A file row's path inside the archive (folders have none)."""
THUMB_SIZE = 96
"""How big a file's thumbnail is drawn in the grid, in pixels."""
HIDDEN_OPACITY = 0.3
"""How faint a hidden file's thumbnail is."""
THUMB_BATCH = 24
"""Thumbnails drawn per worker job: the first show soon, and closing the dialog stops it."""


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
    thumbnails_drawn = Signal()
    """Every file's thumbnail that can be drawn is shown."""
    thumbnails_toggled = Signal(bool)
    """**Show thumbnails** was ticked or unticked (the window remembers it)."""

    def __init__(
        self,
        current: ArchiveChoices,
        archive_path: str,
        extensions: Collection[str] | None,
        pool: QThreadPool | None = None,
        parent: QWidget | None = None,
        thumbnails: ThumbnailResolver | None = None,
        show_thumbnails: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("choose_inside")
        self.current = current
        self._pool = pool
        self._resolver = thumbnails
        self._pictures: dict[str, QPixmap] = {}
        """Thumbnails drawn so far, by path inside the archive."""
        self._to_draw: list[str] | None = None
        """Files whose thumbnails are still to draw (``None``: not started)."""
        self._drawing = False
        self.setWindowTitle(f"Choose what to read inside {current.name}")
        self.resize(560, 520)
        self._extensions = None if extensions is None else {e.lower() for e in extensions}
        self.files: dict[str, MemberEntry] = {}
        """The files listed, by path inside the archive."""
        self.left_out: list[str] = []
        """Files the theme doesn't read here, not listed."""
        self._hidden: set[str] = set()
        self.error: str | None = None
        self._tip: dict[str, str] = {}
        """A tile's tooltip's first line, by path: the path and size."""

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
        self.grid_model = QStandardItemModel(self)
        """The files alone, one tile each, for the grid."""
        self.grid = QListView()
        self.grid.setObjectName("inside_grid")
        self.grid.setModel(self.grid_model)
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setWordWrap(True)
        self.grid.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.grid.setGridSize(QSize(THUMB_SIZE + 32, THUMB_SIZE + 44))
        self.grid.setSpacing(4)
        self.grid.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.grid.clicked.connect(self._tile_clicked)
        self.views = QStackedWidget()
        self.views.addWidget(self.tree)
        self.views.addWidget(self.grid)
        self.show_hidden = QCheckBox("Show hidden files")
        self.show_hidden.setObjectName("show_hidden")
        self.show_hidden.setChecked(True)
        self.show_hidden.setToolTip("Off: tuck hidden files away, to see what will be read")
        self.show_hidden.toggled.connect(lambda _: self._restyle())
        self.show_thumbnails = QCheckBox("Show thumbnails")
        self.show_thumbnails.setObjectName("show_thumbnails")
        self.show_thumbnails.setToolTip("Draw each file Tagalot can: pictures, fonts, PDFs...")
        self.show_thumbnails.setChecked(show_thumbnails and thumbnails is not None)
        self.show_thumbnails.setVisible(thumbnails is not None)
        self.show_thumbnails.toggled.connect(self._thumbnails_toggled)
        self.show_all = QPushButton("Show all")
        self.show_all.setObjectName("show_all")
        self.show_all.setToolTip("Read every file listed; then hide the few to leave out")
        self.show_all.clicked.connect(lambda: self.set_hidden(self.files, False))
        self.hide_all = QPushButton("Hide all")
        self.hide_all.setObjectName("hide_all")
        self.hide_all.setToolTip("Hide every file; then show the few to read")
        self.hide_all.clicked.connect(lambda: self.set_hidden(self.files, True))
        for button in (self.show_all, self.hide_all):
            button.setAutoDefault(False)
            button.setEnabled(False)
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
        layout.addWidget(self.views, 1)
        options = QHBoxLayout()
        options.addWidget(self.show_hidden)
        options.addWidget(self.show_thumbnails)
        options.addStretch(1)
        options.addWidget(self.show_all)
        options.addWidget(self.hide_all)
        layout.addLayout(options)
        layout.addWidget(self.summary)
        layout.addWidget(self.buttons)

        self._show_view()
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
        self.show_all.setEnabled(bool(self.files))
        self.hide_all.setEnabled(bool(self.files))
        if self.show_thumbnails.isChecked():
            self._draw_thumbnails()
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
            size = format_bytes(self.files[path].size)
            folder(parent).appendRow(self._row(item, size))
            tile = QStandardItem(self._placeholder(), name)
            tile.setData(path, PATH)
            tile.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            self.grid_model.appendRow(tile)
            self._tip[path] = f"{path} ({size})"
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

    # --- thumbnails ---

    def _thumbnails_toggled(self, on: bool) -> None:
        self._show_view()
        if on and self.files:
            self._draw_thumbnails()
        self.thumbnails_toggled.emit(on)

    def _show_view(self) -> None:
        """The grid of thumbnails when the box is ticked, else the tree."""
        on = self.show_thumbnails.isChecked()
        self.views.setCurrentWidget(self.grid if on else self.tree)
        if on and self.width() < 760:
            self.resize(760, max(self.height(), 620))

    def _placeholder(self) -> QIcon:
        return self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)

    def _tile_icon(self, path: str, hidden: bool) -> QIcon:
        """A tile's picture: its thumbnail (faint when hidden), else a file icon."""
        picture = self._pictures.get(path)
        if picture is None:
            return self._placeholder()
        if not hidden:
            return QIcon(picture)
        faint = QPixmap(picture.size())
        faint.fill(Qt.GlobalColor.transparent)
        painter = QPainter(faint)
        painter.setOpacity(HIDDEN_OPACITY)
        painter.drawPixmap(0, 0, picture)
        painter.end()
        return QIcon(faint)

    def tiles(self) -> dict[str, QStandardItem]:
        """The grid's tiles, by path inside the archive."""
        found = {}
        for row in range(self.grid_model.rowCount()):
            tile = self.grid_model.item(row)
            if tile is not None:
                found[str(tile.data(PATH))] = tile
        return found

    def _draw_thumbnails(self) -> None:
        """Draw the files' thumbnails a batch at a time, while the box stays ticked."""
        if self._to_draw is None:
            self._to_draw = [p for p in sorted(self.files) if member_renderer(p) is not None]
        if self._drawing:
            return
        if not self._to_draw or not self.show_thumbnails.isChecked():
            if not self._to_draw:
                self.thumbnails_drawn.emit()
            return
        resolver = self._resolver
        assert resolver is not None
        batch, self._to_draw = self._to_draw[:THUMB_BATCH], self._to_draw[THUMB_BATCH:]
        archive_id = self.current.archive_id
        self._drawing = True

        def job() -> list[tuple[str, QImage]]:
            found = []
            for name, thumb in resolver.archive_thumbnails(archive_id, batch):
                image = None if thumb is None else QImage.fromData(thumb.data)
                if image is not None and not image.isNull():
                    found.append((name, image))
            return found

        run_in_pool(job, on_done=self._drawn, on_error=self._not_drawn, pool=self._pool)

    def _drawn(self, images: list[tuple[str, QImage]]) -> None:
        if not shiboken6.isValid(self):
            return
        self._drawing = False
        tiles = self.tiles()
        for name, image in images:
            self._pictures[name] = QPixmap.fromImage(image)
            if name in tiles:
                tiles[name].setIcon(self._tile_icon(name, name in self._hidden))
        self._draw_thumbnails()

    def _not_drawn(self, error: BaseException) -> None:
        if shiboken6.isValid(self):
            self._drawing = False
            self._to_draw = []  # the archive can't be read: stop trying
            self.thumbnails_drawn.emit()

    # --- hiding ---

    def _clicked(self, index: QModelIndex) -> None:
        if index.column() == HIDE:
            self.toggle(index.siblingAtColumn(NAME))

    def _tile_clicked(self, index: QModelIndex) -> None:
        path = index.data(PATH)
        if isinstance(path, str):
            self.set_hidden({path}, path not in self._hidden)

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
        for path, tile in self.tiles().items():
            hidden = path in self._hidden
            font = tile.font()
            font.setStrikeOut(hidden)
            tile.setFont(font)
            tile.setForeground(dim if hidden else plain)
            tile.setIcon(self._tile_icon(path, hidden))
            click = "Click to show it again" if hidden else "Click to hide it: don't read it"
            tile.setToolTip(f"{self._tip.get(path, path)}\n{click}")
            self.grid.setRowHidden(tile.row(), hiding and hidden)
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
