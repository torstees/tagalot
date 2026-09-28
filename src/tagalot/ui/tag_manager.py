"""The tag manager: the full tag tree with usage counts and a details pane (DESIGN.md §12).

```
Tag manager
[Filter tags…     ]
 Tag            Items  With sub-tags  │  Iceland
 ▾ Places           0              5  │  Places > Iceland
     Iceland        3              3  │  Color: ■ #2e86de
     Beach          2              2  │  Summer 2019 road trip…
                                      │  Also called: Ísland
```

The tree and its counts are loaded in a worker; :meth:`TagManagerPage.reload` refreshes
them after tags change. Editing (rename, move, merge, delete, colors, aliases, descriptions)
comes with later tasks.
"""

from typing import Any

import shiboken6
from PySide6.QtCore import QModelIndex, QObject, QPersistentModelIndex, Qt, QThreadPool
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.session import KeepSession
from tagalot.core.tags import PATH_SEPARATOR, TagTree, TagUsage, tag_usage
from tagalot.ui.models.tag_tree import TagTreeModel
from tagalot.ui.workers import run_in_pool

AnyIndex = QModelIndex | QPersistentModelIndex

ITEMS, WITH_SUBTAGS = 1, 2
HEADERS = ("Tag", "Items", "With sub-tags")


class TagManagerModel(TagTreeModel):
    """The tag tree with two count columns: items tagged directly, and items tagged with
    the tag or any tag under it (each counted once)."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.usage: dict[int, TagUsage] = {}

    def set_usage(self, usage: dict[int, TagUsage]) -> None:
        self.usage = dict(usage)
        for tag_id in self._row:  # every shown row, nested ones too
            self.dataChanged.emit(self.index_at(tag_id, ITEMS), self.index_at(tag_id, WITH_SUBTAGS))

    def index_at(self, tag_id: int, column: int) -> QModelIndex:
        """The index of a shown tag in ``column``."""
        index = self.index_of(tag_id)
        return index.siblingAtColumn(column) if index.isValid() else index

    def columnCount(self, parent: AnyIndex = QModelIndex()) -> int:  # noqa: B008
        return len(HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation != Qt.Orientation.Horizontal or not 0 <= section < len(HEADERS):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole and section:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def data(self, index: AnyIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if index.column() == 0:
            return super().data(index, role)
        tag_id = self.tag_id(index)
        if tag_id is None:
            return None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.DisplayRole:
            usage = self.usage.get(tag_id)
            if usage is None:
                return ""
            count = usage.direct if index.column() == ITEMS else usage.with_subtags
            return f"{count:,}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return (
                "Items tagged with this tag itself"
                if index.column() == ITEMS
                else "Items tagged with this tag or any tag under it, each counted once"
            )
        return None


class TagDetails(QFrame):
    """The selected tag's details (read-only for now)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.title = QLabel()
        font = QFont(self.title.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 2)
        self.title.setFont(font)
        self.path = QLabel()
        self.color = QLabel()
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.aliases = QLabel()
        self.aliases.setWordWrap(True)
        self.counts = QLabel()
        self.empty = QLabel("Select a tag to see its details.")
        form = QFormLayout()
        form.addRow("Path:", self.path)
        form.addRow("Color:", self.color)
        form.addRow("Description:", self.description)
        form.addRow("Also called:", self.aliases)
        form.addRow("Used on:", self.counts)
        self.fields = QWidget()
        self.fields.setLayout(form)
        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.fields)
        layout.addWidget(self.empty)
        layout.addStretch(1)
        self.show_tag(None, None, None)

    def show_tag(self, tree: TagTree | None, tag_id: int | None, usage: TagUsage | None) -> None:
        shown = tree is not None and tag_id is not None and tag_id in tree
        self.title.setVisible(shown)
        self.fields.setVisible(shown)
        self.empty.setVisible(not shown)
        if not shown:
            return
        assert tree is not None
        assert tag_id is not None
        node = tree.node(tag_id)
        self.title.setText(node.name)
        self.path.setText(PATH_SEPARATOR.join(tree.path(tag_id)))
        if node.color:
            self.color.setText(f'<span style="color:{node.color}">■</span> {node.color}')
        else:
            self.color.setText("None")
        self.description.setText(node.description or "None")
        self.aliases.setText(", ".join(tree.aliases(tag_id)) or "None")
        if usage is None:
            self.counts.setText("…")
        else:
            items = "item" if usage.direct == 1 else "items"
            self.counts.setText(
                f"{usage.direct:,} {items} directly; {usage.with_subtags:,} with its sub-tags"
            )


class TagManagerPage(QWidget):
    """The Tag manager page: filter, tree with counts, and the selected tag's details."""

    def __init__(
        self,
        session: KeepSession,
        *,
        pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self._pool = pool
        self._expanded: set[int] = set()
        self._restoring = False  # true while the page itself re-applies folds
        self.loaded = False

        heading = QLabel("Tag manager")
        font = QFont(heading.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 2)
        heading.setFont(font)
        self.status = QLabel("Loading tags…")
        header_row = QHBoxLayout()
        header_row.addWidget(heading)
        header_row.addStretch(1)
        header_row.addWidget(self.status)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter tags…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._filter_changed)

        self.model = TagManagerModel(self)
        self.view = QTreeView()
        self.view.setModel(self.model)
        self.view.setUniformRowHeights(True)
        self.view.setAlternatingRowColors(True)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.view.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (ITEMS, WITH_SUBTAGS):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.view.expanded.connect(lambda i: self._fold(i, True))
        self.view.collapsed.connect(lambda i: self._fold(i, False))
        self.view.selectionModel().currentChanged.connect(lambda *_: self._show_details())

        self.details = TagDetails()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.filter_edit)
        left_layout.addWidget(self.view, 1)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addLayout(header_row)
        layout.addWidget(splitter, 1)
        self.reload()

    # --- loading ---

    def reload(self) -> None:
        """Load the tag tree and usage counts in a worker, keeping folds and the selection."""
        session = self.session

        def load() -> tuple[TagTree, dict[int, TagUsage]]:
            tree = session.tag_cache.get()
            with session.reader.connect() as conn:
                return tree, tag_usage(conn, tree)

        def loaded(result: tuple[TagTree, dict[int, TagUsage]]) -> None:
            if shiboken6.isValid(self):
                self.set_tree(*result)

        run_in_pool(load, on_done=loaded, pool=self._pool)

    def set_tree(self, tree: TagTree, usage: dict[int, TagUsage]) -> None:
        current = self.current_tag()
        if not self.loaded:
            self._expanded = set(tree.children(None))  # start with the top level open
        self._expanded &= set(tree)
        self.loaded = True
        self.model.set_tree(tree)
        self.model.set_usage(usage)
        self._restore_folds()
        if current is not None and current in tree:
            self.select_tag(current)
        count = len(tree)
        self.status.setText("No tags yet" if not count else f"{count:,} tag{'s' * (count != 1)}")
        self._show_details()

    # --- selection and folds ---

    def current_tag(self) -> int | None:
        return self.model.tag_id(self.view.currentIndex())

    def select_tag(self, tag_id: int) -> bool:
        index = self.model.index_of(tag_id)
        if not index.isValid():
            return False
        self.view.setCurrentIndex(index)
        self.view.scrollTo(index)
        return True

    def _show_details(self) -> None:
        tag_id = self.current_tag()
        usage = self.model.usage.get(tag_id) if tag_id is not None else None
        self.details.show_tag(self.model.tree, tag_id, usage)

    def _filter_changed(self, text: str) -> None:
        current = self.current_tag()
        self.model.set_filter(text)
        self._restore_folds()
        if current is not None:
            self.select_tag(current)
        self._show_details()

    def _restore_folds(self) -> None:
        self._restoring = True
        try:
            if self.model.filtering:
                self.view.expandAll()
            else:
                for tag_id in self._expanded:
                    index = self.model.index_of(tag_id)
                    if index.isValid():
                        self.view.expand(index)
        finally:
            self._restoring = False

    def _fold(self, index: QModelIndex, expanded: bool) -> None:
        if self._restoring or self.model.filtering:
            return
        tag_id = self.model.tag_id(index)
        if tag_id is None:
            return
        if expanded:
            self._expanded.add(tag_id)
        else:
            self._expanded.discard(tag_id)
