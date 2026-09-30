"""The tree layout: results whose containers expand to show what they hold (DESIGN.md §8
"Show contained", §12).

```
▾ Aurora Studio      Artist
    dusk sky.png     Image
    ▸ …
▸ Kenji Sato         Artist
  mood board.png     Image
```

The top level is the search's own results, straight from the list's lazily paged
:class:`~tagalot.ui.models.results.ResultsModel`. A container's children load in a worker
the first time it is expanded (:func:`~tagalot.core.search.child_hits`): everything it
directly holds, by title, without the excluded items. Children can be containers too.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import shiboken6
from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QRect,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QAbstractItemView, QFrame, QTreeView, QWidget

from tagalot.core.search import SearchHit, child_hits
from tagalot.core.search_fields import contained_types
from tagalot.core.session import KeepSession
from tagalot.ui.dnd import dragged_tags
from tagalot.ui.models.results import ResultsModel, Row, cell_text, cell_tooltip, row_values
from tagalot.ui.result_table import OUTLINE_COLOR
from tagalot.ui.workers import run_in_pool

AnyIndex = QModelIndex | QPersistentModelIndex


@dataclass
class _Node:
    """A row that has been looked at: where it is, and its children once loaded."""

    id: int
    parent: int
    """The parent node's id; 0 for the top level."""
    row: int
    hit: SearchHit
    children: list[Row] | None = None
    loading: bool = False


class ResultTreeModel(QAbstractItemModel):
    """The search results as a tree over a :class:`ResultsModel` (its top level)."""

    def __init__(
        self,
        source: ResultsModel,
        session: KeepSession,
        pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.source = source
        self.session = session
        self._pool = pool
        self._nodes: dict[int, _Node] = {}
        self._keys: dict[tuple[int, int], int] = {}
        self._next_id = 1
        self._generation = 0
        schema = session.schema
        self._containers = {
            t.type_id for t in schema.entities.values() if contained_types(schema, t.type_id)
        }
        source.modelAboutToBeReset.connect(self.beginResetModel)
        source.modelReset.connect(self._source_reset)
        source.dataChanged.connect(self._source_changed)
        source.headerDataChanged.connect(self.headerDataChanged)

    # --- nodes ---

    def node(self, index: AnyIndex) -> _Node | None:
        """The node of a valid index (``None`` for a top-level row not loaded yet)."""
        return self._node(int(index.internalId()), index.row()) if index.isValid() else None

    def hit(self, index: AnyIndex) -> SearchHit | None:
        found = self.node(index)
        return found.hit if found is not None else None

    def _node(self, parent_id: int, row: int) -> _Node | None:
        known = self._keys.get((parent_id, row))
        if known is not None:
            return self._nodes[known]
        if parent_id == 0:
            hit = self.source.hit(row)
        else:
            parent = self._nodes.get(parent_id)
            children = parent.children if parent is not None else None
            hit = children[row][0] if children is not None and row < len(children) else None
        if hit is None:
            return None
        node = _Node(self._next_id, parent_id, row, hit)
        self._next_id += 1
        self._nodes[node.id] = node
        self._keys[(parent_id, row)] = node.id
        return node

    def _row(self, index: AnyIndex) -> Row | None:
        """A child row's hit and values (top-level rows come from the source)."""
        parent = self._nodes.get(int(index.internalId()))
        if parent is None or parent.children is None or index.row() >= len(parent.children):
            return None
        return parent.children[index.row()]

    # --- QAbstractItemModel ---

    def index(self, row: int, column: int, parent: AnyIndex = QModelIndex()) -> QModelIndex:  # noqa: B008
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, column, 0)
        node = self.node(parent)
        return self.createIndex(row, column, node.id) if node is not None else QModelIndex()

    def parent(self, index: AnyIndex = QModelIndex()) -> QModelIndex:  # type: ignore[override]  # noqa: B008
        if not index.isValid():
            return QModelIndex()
        parent = self._nodes.get(int(index.internalId()))
        if parent is None:
            return QModelIndex()
        return self.createIndex(parent.row, 0, parent.parent)

    def rowCount(self, parent: AnyIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.column() > 0:
            return 0
        if not parent.isValid():
            return self.source.rowCount()
        node = self.node(parent)
        return len(node.children) if node is not None and node.children is not None else 0

    def columnCount(self, parent: AnyIndex = QModelIndex()) -> int:  # noqa: B008
        return self.source.columnCount()

    def hasChildren(self, parent: AnyIndex = QModelIndex()) -> bool:  # noqa: B008
        if not parent.isValid():
            return self.source.rowCount() > 0
        if parent.column() > 0:
            return False
        node = self.node(parent)
        if node is None or node.hit.type not in self._containers:
            return False
        return node.children is None or bool(node.children)

    def canFetchMore(self, parent: AnyIndex) -> bool:
        if not parent.isValid():
            return False
        node = self.node(parent)
        return (
            node is not None
            and node.children is None
            and not node.loading
            and node.hit.type in self._containers
        )

    def fetchMore(self, parent: AnyIndex) -> None:
        node = self.node(parent)
        if node is None or node.children is not None or node.loading:
            return
        spec = self.source.spec
        if spec is None:
            return
        node.loading = True
        generation, session, columns = self._generation, self.session, self.source.columns
        parent_id = node.hit.id

        def job() -> list[Row]:
            tree = session.tag_cache.get()
            with session.reader.connect() as conn:
                hits = child_hits(conn, spec, tree, parent_id)
                values = row_values(conn, session.schema, tree, hits, columns)
            return [(h, values.get(h.id, {})) for h in hits]

        def done(rows: list[Row]) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._loaded(node.id, rows)

        run_in_pool(job, on_done=done, pool=self._pool)

    def _loaded(self, node_id: int, rows: list[Row]) -> None:
        node = self._nodes.get(node_id)
        if node is None:
            return
        node.loading = False
        index = self.createIndex(node.row, 0, node.parent)
        if rows:
            self.beginInsertRows(index, 0, len(rows) - 1)
            node.children = rows
            self.endInsertRows()
        else:
            node.children = []
            self.dataChanged.emit(index, index)  # no expand arrow any more

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        return self.source.headerData(section, orientation, role)

    def data(self, index: AnyIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        if int(index.internalId()) == 0:
            return self.source.data(self.source.index(index.row(), index.column()), role)
        row = self._row(index)
        columns = self.source.columns
        if row is None or not 0 <= index.column() < len(columns):
            return None
        column = columns[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return cell_text(column, row, self.source.type_labels)
        if role == Qt.ItemDataRole.ToolTipRole:
            return cell_tooltip(column, row, self.source.type_labels)
        if role == Qt.ItemDataRole.TextAlignmentRole and column.numeric:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def flags(self, index: AnyIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # --- following the source ---

    def _source_reset(self) -> None:
        self._generation += 1
        self._nodes.clear()
        self._keys.clear()
        self.endResetModel()

    def _source_changed(self, top_left: QModelIndex, bottom_right: QModelIndex) -> None:
        self.dataChanged.emit(
            self.index(top_left.row(), top_left.column()),
            self.index(bottom_right.row(), bottom_right.column()),
        )


class ResultTree(QTreeView):
    """The tree of results. Accepts tags dragged from the tagging panel, like the list:
    emits :attr:`tags_dropped` with entity ids and tag ids."""

    tags_dropped = Signal(list, list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDropIndicatorShown(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._outline = QFrame(self.viewport())
        self._outline.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._outline.setObjectName("drop_outline")
        self._outline.setStyleSheet(f"#drop_outline {{ border: 2px dashed {OUTLINE_COLOR}; }}")
        self._outline.hide()

    def tree_model(self) -> ResultTreeModel:
        model = self.model()
        assert isinstance(model, ResultTreeModel)
        return model

    def selected_hits(self) -> list[SearchHit]:
        model = self.tree_model()
        return [
            hit
            for index in self.selectionModel().selectedRows()
            if (hit := model.hit(index)) is not None
        ]

    def drop_targets(self, point: QPoint) -> list[SearchHit]:
        """What a drop at ``point`` tags: the selection if dropped on it, else that row."""
        index = self.indexAt(point)
        hit = self.tree_model().hit(index) if index.isValid() else None
        if hit is None:
            return []
        selected = self.selected_hits()
        return selected if hit.id in {h.id for h in selected} else [hit]

    def set_columns_hidden(self, keys: Sequence[str], hidden: set[str]) -> None:
        for i, key in enumerate(keys):
            self.setColumnHidden(i, key != "title" and key in hidden)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if dragged_tags(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        point = event.position().toPoint()
        targets = self.drop_targets(point) if dragged_tags(event.mimeData()) else []
        if targets:
            rect = self.visualRect(self.indexAt(point))
            self._outline.setGeometry(QRect(0, rect.top(), self.viewport().width(), rect.height()))
            self._outline.show()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            self._outline.hide()
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._outline.hide()

    def dropEvent(self, event: QDropEvent) -> None:
        self._outline.hide()
        tags = dragged_tags(event.mimeData())
        targets = self.drop_targets(event.position().toPoint())
        if not tags or not targets:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.tags_dropped.emit([h.id for h in targets], tags)
