"""The tag tree as a Qt model, with a filter (DESIGN.md §12 "Tagging panel").

The model shows a :class:`~tagalot.core.tags.TagTree` snapshot. Filtering keeps the tags
whose name or an alias contains the text, plus their ancestors, so every match is shown in
place in the hierarchy. Matches are drawn in bold; ancestors shown only for context are not.
The whole tree is small (hundreds to low thousands of tags), so a new snapshot or filter
simply resets the model.
"""

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import (
    QAbstractItemModel,
    QMimeData,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap

from tagalot.core.tags import PATH_SEPARATOR, TagTree, description_excerpt, search_key
from tagalot.ui.dnd import TAG_MIME, tags_mime

TAG_ID_ROLE = Qt.ItemDataRole.UserRole + 1
"""The tag id of an index."""

EXCERPT_WIDTH = 32
"""Characters of a description shown next to a tag matched only through it."""


def tag_tooltip(tree: TagTree, tag_id: int) -> str:
    """A tag's tooltip: its path, its description, and its aliases."""
    node = tree.node(tag_id)
    lines = [PATH_SEPARATOR.join(tree.path(tag_id))]
    if node.description:
        lines.append(node.description)
    if aliases := tree.aliases(tag_id):
        lines.append("Also: " + ", ".join(aliases))
    return "\n".join(lines)


AnyIndex = QModelIndex | QPersistentModelIndex


class TagTreeModel(QAbstractItemModel):
    """One column: tag names in tree order. Each index's internal id is its tag id.

    With items selected (:meth:`set_selection`), each tag has a check: checked if every
    selected item has it, partly checked if some do, unchecked if none do. Clicking a check
    doesn't change the model: it emits :attr:`check_clicked` with the tag id and whether to
    remove it (it was on all of them) or apply it to all; the summary is updated once the
    tagging is done.
    """

    check_clicked = Signal(int, bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.tree: TagTree | None = None
        self.filter_text = ""
        self._visible: frozenset[int] | None = None  # None: every tag
        self._matches: frozenset[int] = frozenset()
        self._alias_hits: dict[int, str] = {}  # matched through an alias, not the name
        self._description_hits: set[int] = set()  # matched only through the description
        self._children: dict[int | None, tuple[int, ...]] = {}
        self._row: dict[int, int] = {}
        self._swatches: dict[str, QPixmap | None] = {}
        self.selected_count = 0
        self._tag_counts: dict[int, int] = {}

    # --- content ---

    def set_tree(self, tree: TagTree) -> None:
        """Show ``tree``, keeping the current filter."""
        self.tree = tree
        self._rebuild()

    def set_filter(self, text: str) -> None:
        """Show only tags whose name or an alias contains ``text``, with their ancestors."""
        self.filter_text = text
        self._rebuild()

    @property
    def filtering(self) -> bool:
        return bool(search_key(self.filter_text))

    def matches(self) -> frozenset[int]:
        """The tags the filter matched (empty when not filtering)."""
        return self._matches

    def visible_count(self) -> int:
        return len(self._row)

    def index_of(self, tag_id: int) -> QModelIndex:
        """The index of a shown tag, or an invalid index."""
        row = self._row.get(tag_id)
        return QModelIndex() if row is None else self.createIndex(row, 0, tag_id)

    def tag_id(self, index: AnyIndex) -> int | None:
        return int(index.internalId()) if index.isValid() else None

    def first_match(self) -> QModelIndex:
        """The first matching tag in tree order (for the keyboard), or an invalid index."""
        for tag_id in self._walk(None):
            if tag_id in self._matches:
                return self.index_of(tag_id)
        return QModelIndex()

    def _walk(self, parent: int | None) -> list[int]:
        order = []
        for child in self._children.get(parent, ()):
            order.append(child)
            order.extend(self._walk(child))
        return order

    def _rebuild(self) -> None:
        self.beginResetModel()
        tree = self.tree
        self._children, self._row = {}, {}
        if tree is None:
            self._visible, self._matches = None, frozenset()
        else:
            if self.filtering:
                self._matches = tree.matching(self.filter_text)
                self._visible = tree.with_ancestors(self._matches)
                needle = search_key(self.filter_text)
                self._alias_hits, self._description_hits = {}, set()
                for tag_id in self._matches:
                    if needle in search_key(tree.node(tag_id).name):
                        continue
                    aliases = [a for a in tree.aliases(tag_id) if needle in search_key(a)]
                    if aliases:
                        self._alias_hits[tag_id] = aliases[0]
                    else:
                        self._description_hits.add(tag_id)
            else:
                self._visible, self._matches = None, frozenset()
                self._alias_hits, self._description_hits = {}, set()
            self._collect(tree, None)
        self.endResetModel()

    def _collect(self, tree: TagTree, parent: int | None) -> None:
        kids = tuple(
            t for t in tree.children(parent) if self._visible is None or t in self._visible
        )
        self._children[parent] = kids
        for row, tag_id in enumerate(kids):
            self._row[tag_id] = row
            self._collect(tree, tag_id)

    # --- QAbstractItemModel ---

    def index(self, row: int, column: int, parent: AnyIndex = QModelIndex()) -> QModelIndex:  # noqa: B008
        kids = self._children.get(self.tag_id(parent), ())
        if column != 0 or not 0 <= row < len(kids):
            return QModelIndex()
        return self.createIndex(row, 0, kids[row])

    def parent(self, index: AnyIndex = QModelIndex()) -> QModelIndex:  # type: ignore[override]  # noqa: B008
        tag_id = self.tag_id(index)
        if tag_id is None or self.tree is None:
            return QModelIndex()
        parent_id = self.tree.node(tag_id).parent_id
        return QModelIndex() if parent_id is None else self.index_of(parent_id)

    def rowCount(self, parent: AnyIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.column() > 0:
            return 0
        return len(self._children.get(self.tag_id(parent), ()))

    def columnCount(self, parent: AnyIndex = QModelIndex()) -> int:  # noqa: B008
        return 1

    def hasChildren(self, parent: AnyIndex = QModelIndex()) -> bool:  # noqa: B008
        return self.rowCount(parent) > 0

    def data(self, index: AnyIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        tag_id = self.tag_id(index)
        tree = self.tree
        if tag_id is None or tree is None:
            return None
        node = tree.node(tag_id)
        if role == Qt.ItemDataRole.DisplayRole:
            alias = self._alias_hits.get(tag_id)
            if alias is not None:
                return f"{node.name} ({alias})"
            if tag_id in self._description_hits and node.description:
                excerpt = description_excerpt(node.description, self.filter_text, EXCERPT_WIDTH)
                return f"{node.name} — {excerpt}"
            return node.name
        if role == TAG_ID_ROLE:
            return tag_id
        if role == Qt.ItemDataRole.CheckStateRole:
            return self.check_state(tag_id)
        if role == Qt.ItemDataRole.ToolTipRole:
            tooltip = tag_tooltip(tree, tag_id)
            if self.selected_count:
                count = self._tag_counts.get(tag_id, 0)
                items = "item" if self.selected_count == 1 else "items"
                tooltip += f"\n\nOn {count} of the {self.selected_count} selected {items}"
            return tooltip
        if role == Qt.ItemDataRole.FontRole and tag_id in self._matches:
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.DecorationRole and node.color:
            return self._swatch(node.color)
        return None

    def flags(self, index: AnyIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = (
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        )
        if self.selected_count:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    # --- the selection summary (DESIGN.md §12) ---

    def set_selection(self, selected_count: int, tag_counts: dict[int, int]) -> None:
        """How many items are selected, and how many of them carry each tag directly."""
        self.selected_count = selected_count
        self._tag_counts = dict(tag_counts) if selected_count else {}
        self._all_rows_changed()

    def check_state(self, tag_id: int) -> Qt.CheckState | None:
        """All, some, or none of the selected items have ``tag_id``; ``None`` if nothing is
        selected."""
        if not self.selected_count:
            return None
        count = self._tag_counts.get(tag_id, 0)
        if count >= self.selected_count:
            return Qt.CheckState.Checked
        return Qt.CheckState.PartiallyChecked if count else Qt.CheckState.Unchecked

    def setData(self, index: AnyIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        tag_id = self.tag_id(index)
        if role != Qt.ItemDataRole.CheckStateRole or tag_id is None or not self.selected_count:
            return False
        # On all of them: remove it from all. On some or none: apply it to all.
        self.check_clicked.emit(tag_id, self.check_state(tag_id) == Qt.CheckState.Checked)
        return False  # the summary changes when the tagging is done

    def _all_rows_changed(self) -> None:
        for tag_id in self._row:
            index = self.index_of(tag_id)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])

    # --- dragging tags onto items (DESIGN.md §12) ---

    def mimeTypes(self) -> list[str]:
        return [TAG_MIME]

    def mimeData(self, indexes: Sequence[QModelIndex]) -> QMimeData:
        ids = [t for i in indexes if (t := self.tag_id(i)) is not None]
        return tags_mime(ids)

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction

    def _swatch(self, color: str) -> QIcon | None:
        """A small square of the tag's color; ``None`` for a color Qt can't parse.

        The pixmap is cached, but each call returns a new ``QIcon``: returning one cached
        icon object from ``data()`` twice crashes PySide6 (6.11), which takes ownership of
        what ``data()`` returns.
        """
        if color not in self._swatches:
            qcolor = QColor(color)
            pixmap = None
            if qcolor.isValid():
                pixmap = QPixmap(10, 10)
                pixmap.fill(qcolor)
            self._swatches[color] = pixmap
        pixmap = self._swatches[color]
        return QIcon(pixmap) if pixmap is not None else None
