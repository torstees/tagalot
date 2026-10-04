"""TOOLS → File keywords (#295, DESIGN.md §7 "File keywords", §12).

```
File keywords                                   3 don't match a tag
Show: [Unmatched ▾]
┌──────────────┬───────┬─────────────────────────────┬───────────────────────┐
│ Keyword      │ Items │ For example                 │ Tag                   │
├──────────────┼───────┼─────────────────────────────┼───────────────────────┤
│ Space opera  │    12 │ Dune, Hyperion, Revelation…  │ —                     │
│ Fantasy      │     9 │ …                           │ Genre > Fantasy       │
└──────────────┴───────┴─────────────────────────────┴───────────────────────┘
[Map to tag…] [Create tag…] [Ignore]
```

The page only reads; its buttons ask the window (:attr:`map_requested`, :attr:`create_requested`,
:attr:`ignore_requested`), which runs each as an undoable step, then :meth:`refresh` shows
the result.
"""

from collections.abc import Sequence
from typing import Any

import shiboken6
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.keywords import KeywordInfo, keyword_report
from tagalot.core.session import KeepSession
from tagalot.core.tags import PATH_SEPARATOR, TagTree
from tagalot.ui.workers import run_in_pool

ModelIndex = QModelIndex | QPersistentModelIndex
SHOWS = ("Unmatched", "Matched", "Ignored", "All")
COLUMNS = ("Keyword", "Items", "For example", "Tag")


def shown(info: KeywordInfo, show: str) -> bool:
    """Whether the page lists ``info`` under the ``show`` choice."""
    if show == "Unmatched":
        return info.tag_id is None and not info.ignored
    if show == "Matched":
        return info.tag_id is not None
    if show == "Ignored":
        return info.ignored and info.tag_id is None
    return True


class KeywordsModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[KeywordInfo] = []
        self.tree: TagTree | None = None

    def set_rows(self, rows: Sequence[KeywordInfo], tree: TagTree) -> None:
        self.beginResetModel()
        self.rows, self.tree = list(rows), tree
        self.endResetModel()

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        info = self.rows[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return info.keyword
            if column == 1:
                return f"{info.items:,}"
            if column == 2:
                more = "…" if info.items > len(info.examples) else ""
                return ", ".join(info.examples) + more
            return self.tag_text(info)
        if role == Qt.ItemDataRole.TextAlignmentRole and column == 1:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.FontRole and column == 3 and info.tag_id is None:
            font = QFont()
            font.setItalic(True)
            return font
        return None

    def tag_text(self, info: KeywordInfo) -> str:
        tree = self.tree
        if info.tag_id is not None and tree is not None and info.tag_id in tree:
            return PATH_SEPARATOR.join(tree.path(info.tag_id))
        return "Ignored" if info.ignored else "—"


class KeywordsPage(QWidget):
    """See the module docstring."""

    map_requested = Signal(list)
    """The selected :class:`KeywordInfo` rows, to map to a tag (one at a time, in order)."""
    create_requested = Signal(object)
    """A :class:`KeywordInfo`: create a tag for it."""
    ignore_requested = Signal(list, bool)
    """Keyword keys, and whether to ignore them (``False``: stop ignoring)."""

    def __init__(self, session: KeepSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.loaded = False
        self._all: list[KeywordInfo] = []
        self._generation = 0

        heading = QLabel("File keywords")
        font = QFont(heading.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 2)
        heading.setFont(font)
        self.status = QLabel("Loading…")
        top = QHBoxLayout()
        top.addWidget(heading)
        top.addStretch(1)
        top.addWidget(self.status)

        self.explain = QLabel(
            "Keywords files give their items (genres, subjects, front matter's tags). A keyword "
            "becomes a tag only by matching one you defined: map it to a tag, create one for it, "
            "or ignore it."
        )
        self.explain.setWordWrap(True)
        self.show_box = QComboBox()
        self.show_box.addItems(SHOWS)
        self.show_box.currentTextChanged.connect(lambda _: self._show())
        show_row = QHBoxLayout()
        show_row.addWidget(QLabel("Show:"))
        show_row.addWidget(self.show_box)
        show_row.addStretch(1)

        self.model = KeywordsModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column in (0, 1, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._enable())
        self.table.doubleClicked.connect(lambda _: self._map())

        self.map_button = QPushButton("Map to tag…")
        self.map_button.setToolTip(
            "Tie the keyword to one of your tags: it becomes the tag's alias"
        )
        self.map_button.clicked.connect(self._map)
        self.create_button = QPushButton("Create tag…")
        self.create_button.setToolTip("Make a tag for the keyword, then tie the keyword to it")
        self.create_button.clicked.connect(self._create)
        self.ignore_button = QPushButton("Ignore")
        self.ignore_button.clicked.connect(self._ignore)
        buttons = QHBoxLayout()
        for button in (self.map_button, self.create_button, self.ignore_button):
            buttons.addWidget(button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.explain)
        layout.addLayout(show_row)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self._enable()
        self.refresh()

    # --- loading ---

    def refresh(self) -> None:
        """Read the keywords again (in a worker), keeping the Show choice."""
        session = self.session
        self._generation += 1
        generation = self._generation

        def load() -> tuple[list[KeywordInfo], TagTree]:
            tree = session.tag_cache.get()
            with session.reader.connect() as conn:
                return keyword_report(conn, tree), tree

        def loaded(result: tuple[list[KeywordInfo], TagTree]) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._all, self._tree = result
                self.loaded = True
                self._show()

        run_in_pool(load, on_done=loaded)

    def _show(self) -> None:
        show = self.show_box.currentText()
        rows = [k for k in self._all if shown(k, show)]
        self.model.set_rows(rows, getattr(self, "_tree", TagTree([], {})))
        unmatched = sum(1 for k in self._all if shown(k, "Unmatched"))
        if not self._all:
            self.status.setText("No file has given keywords yet")
        elif unmatched:
            self.status.setText(f"{unmatched:,} don't match a tag")
        else:
            self.status.setText("Every keyword matches a tag or is ignored")
        self._enable()

    # --- actions ---

    def selected(self) -> list[KeywordInfo]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.rows[r] for r in rows]

    def _enable(self) -> None:
        chosen = self.selected()
        self.map_button.setEnabled(bool(chosen))
        self.create_button.setEnabled(len(chosen) == 1)
        ignoring = bool(chosen) and all(k.ignored for k in chosen)
        self.ignore_button.setText("Stop ignoring" if ignoring else "Ignore")
        self.ignore_button.setEnabled(bool(chosen) and all(k.tag_id is None for k in chosen))

    def _map(self) -> None:
        chosen = self.selected()
        if chosen:
            self.map_requested.emit(chosen)

    def _create(self) -> None:
        chosen = self.selected()
        if len(chosen) == 1:
            self.create_requested.emit(chosen[0])

    def _ignore(self) -> None:
        chosen = self.selected()
        if chosen:
            ignoring = not all(k.ignored for k in chosen)
            self.ignore_requested.emit([k.key for k in chosen], ignoring)
