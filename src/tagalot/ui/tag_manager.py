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
them after tags change.

Editing (§7): **New tag…** / **New sub-tag…** ask for a name; a tag is renamed in place (F2,
double-click, or **Rename**); it moves by dragging it onto another tag (or onto empty space,
for the top level) or with **Move to…**. The page only asks: it emits
:attr:`TagManagerPage.add_requested` and friends, and the window runs the operation.
Merge, delete, colors, aliases, and descriptions come with later tasks.
"""

from typing import Any

import shiboken6
from PySide6.QtCore import (
    QMimeData,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.session import KeepSession
from tagalot.core.tags import PATH_SEPARATOR, TagTree, TagUsage, tag_usage
from tagalot.ui.dnd import dragged_tags
from tagalot.ui.models.tag_tree import TagTreeModel
from tagalot.ui.tag_picker import MoveToDialog
from tagalot.ui.workers import run_in_pool

AnyIndex = QModelIndex | QPersistentModelIndex

ITEMS, WITH_SUBTAGS = 1, 2
HEADERS = ("Tag", "Items", "With sub-tags")


class TagManagerModel(TagTreeModel):
    """The tag tree with two count columns: items tagged directly, and items tagged with
    the tag or any tag under it (each counted once).

    Names are editable (a rename emits :attr:`rename_requested`; the model changes once the
    rename is done), and tags dropped on a tag or on empty space emit :attr:`move_requested`.
    """

    rename_requested = Signal(int, str)
    move_requested = Signal(list, object)
    """Dropped tag ids, and the new parent (``None``: the top level)."""

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

    # --- editing: rename in place, move by dropping ---

    def flags(self, index: AnyIndex) -> Qt.ItemFlag:
        flags = super().flags(index) | Qt.ItemFlag.ItemIsDropEnabled
        if index.isValid() and index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index: AnyIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        tag_id = self.tag_id(index)
        if role == Qt.ItemDataRole.EditRole and tag_id is not None and index.column() == 0:
            if str(value).strip():
                self.rename_requested.emit(tag_id, str(value))
            return False  # the name changes when the rename is done
        return super().setData(index, value, role)

    # Dragging a tag within the tree moves it; dragged onto results, the same drag tags
    # them (a copy). Both ends must allow both actions, or Qt refuses the drop.
    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def canDropMimeData(
        self, data: QMimeData, action: Qt.DropAction, row: int, column: int, parent: AnyIndex
    ) -> bool:
        return bool(dragged_tags(data))

    def dropMimeData(
        self, data: QMimeData, action: Qt.DropAction, row: int, column: int, parent: AnyIndex
    ) -> bool:
        tags = dragged_tags(data)
        if not tags:
            return False
        # On a tag: under it. Between rows or on empty space: under that level's parent.
        self.move_requested.emit(tags, self.tag_id(parent))
        return False  # nothing changes here; the tree reloads once the move is done

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


def ask_name(parent: QWidget, title: str, label: str, text: str = "") -> str | None:
    """Ask for a tag name; ``None`` if cancelled or blank. (Tests replace this.)"""
    name, ok = QInputDialog.getText(parent, title, label, text=text)
    return name.strip() or None if ok else None


class TagManagerPage(QWidget):
    """The Tag manager page: filter, tree with counts, and the selected tag's details.

    Signals (the window runs the operations): :attr:`add_requested` (parent id or ``None``,
    name), :attr:`rename_requested` (tag id, new name, old name), :attr:`move_requested`
    (tag id, new parent id or ``None``).
    """

    add_requested = Signal(object, str)
    rename_requested = Signal(int, str, str)
    move_requested = Signal(int, object)

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
        self.model.rename_requested.connect(self._rename_requested)
        self.model.move_requested.connect(self._dropped)
        self.view = QTreeView()
        self.view.setModel(self.model)
        self.view.setUniformRowHeights(True)
        self.view.setAlternatingRowColors(True)
        self.view.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setDragEnabled(True)
        self.view.setAcceptDrops(True)
        self.view.setDropIndicatorShown(True)
        self.view.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.view.setDefaultDropAction(Qt.DropAction.MoveAction)
        header = self.view.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (ITEMS, WITH_SUBTAGS):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.view.expanded.connect(lambda i: self._fold(i, True))
        self.view.collapsed.connect(lambda i: self._fold(i, False))
        self.view.selectionModel().currentChanged.connect(lambda *_: self._on_current())

        self.new_button = QPushButton("New tag…")
        self.new_button.setToolTip("Create a tag at the top level")
        self.new_button.clicked.connect(lambda: self.new_tag(under_current=False))
        self.new_child_button = QPushButton("New sub-tag…")
        self.new_child_button.setToolTip("Create a tag under the selected one")
        self.new_child_button.clicked.connect(lambda: self.new_tag(under_current=True))
        self.rename_button = QPushButton("Rename")
        self.rename_button.setToolTip("Rename the selected tag (F2, or double-click it)")
        self.rename_button.clicked.connect(self.start_rename)
        self.move_button = QPushButton("Move to…")
        self.move_button.setToolTip(
            "Move the selected tag, with its sub-tags (you can also drag it onto another tag)"
        )
        self.move_button.clicked.connect(self.move_current)
        buttons = QHBoxLayout()
        for button in (
            self.new_button,
            self.new_child_button,
            self.rename_button,
            self.move_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)

        self.details = TagDetails()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addLayout(buttons)
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
        self._on_current()
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

    # --- editing (the window runs the operations) ---

    def new_tag(self, *, under_current: bool) -> None:
        """Ask for a name and request a new tag (under the selected tag, or at the top)."""
        tree = self.model.tree
        parent = self.current_tag() if under_current else None
        if tree is None or (under_current and parent is None):
            return
        where = (
            f"under \u201c{tree.node(parent).name}\u201d"
            if parent is not None
            else "at the top level"
        )
        name = ask_name(self, "New tag", f"Name of the new tag {where}:")
        if name:
            self.add_requested.emit(parent, name)

    def start_rename(self) -> None:
        index = self.view.currentIndex()
        if index.isValid():
            self.view.edit(index.siblingAtColumn(0))

    def move_current(self) -> None:
        """Pick a new parent for the selected tag with the Move to… picker."""
        tag_id, tree = self.current_tag(), self.model.tree
        if tag_id is None or tree is None:
            return
        dialog = MoveToDialog(tree, tag_id, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.move_requested.emit(tag_id, dialog.target())

    def _rename_requested(self, tag_id: int, name: str) -> None:
        tree = self.model.tree
        if tree is not None and tag_id in tree:
            self.rename_requested.emit(tag_id, name, tree.node(tag_id).name)

    def _dropped(self, tag_ids: list[int], parent_id: int | None) -> None:
        tree = self.model.tree
        if tree is None:
            return
        for tag_id in tag_ids:
            if tag_id in tree and tree.node(tag_id).parent_id != parent_id:
                self.move_requested.emit(tag_id, parent_id)

    def _on_current(self) -> None:
        has_tag = self.current_tag() is not None
        for button in (self.new_child_button, self.rename_button, self.move_button):
            button.setEnabled(has_tag)
        self._show_details()

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
