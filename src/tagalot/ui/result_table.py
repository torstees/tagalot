"""The list layout's columns and table setup, shared by search pages and grouped sections."""

from collections.abc import Iterable, Sequence

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QMenu,
    QTableView,
    QWidget,
)

from tagalot.core.search_fields import scope_fields, scoped_tables
from tagalot.core.theme_schema import ThemeSchema
from tagalot.ui.dnd import dragged_tags
from tagalot.ui.models.results import KEYWORDS, TAGS, ResultColumn

TITLE_MIN_WIDTH = 200
TAGS_WIDTH = 220
DEFAULT_HIDDEN = frozenset({TAGS, KEYWORDS})
"""Columns hidden until the user shows them (right-click a column header)."""
OUTLINE_COLOR = "#f0a020"
"""The drop target's dashed outline: amber, distinct from the selection color."""
COLUMN_WIDTH = 140
NUMERIC_WIDTH = 100


def list_columns(
    schema: ThemeSchema, types: Sequence[str], keywords: bool = False
) -> list[ResultColumn]:
    """The list layout's columns for a scope: the title (named as the types call it), the
    type when several types are in scope, the item's tags (hidden unless the user shows
    them, :data:`DEFAULT_HIDDEN`; next to the title so they stay in view when shown), then
    the card fields every scoped type has."""
    tables = scoped_tables(schema, types)
    labels = {t.entity.title_label for t in tables}
    columns = [ResultColumn("title", labels.pop() if len(labels) == 1 else "Title")]
    if len(tables) != 1:
        columns.append(ResultColumn("type", "Type", sortable=False))
    columns.append(ResultColumn(TAGS, "Tags", sortable=False))
    if keywords:  # only in keeps whose files give keywords (§7)
        columns.append(ResultColumn(KEYWORDS, "Keywords", sortable=False))
    columns.extend(
        ResultColumn(f.name, f.spec.label, numeric=f.type in (int, float), display=f.spec.display)
        for f in scope_fields(schema, types)
        if f.spec.card
    )
    return columns


def count_text(total: int) -> str:
    return f"{total:,} item" if total == 1 else f"{total:,} items"


class ResultTable(QTableView):
    """A results table that accepts tags dragged from the tagging panel (§12).

    Dropping on a selected row tags the whole selection; dropping on any other row tags just
    that row. While dragging, the rows that would be tagged are outlined. Emits
    :attr:`tags_dropped` with the target rows and the tag ids; the page does the tagging.
    """

    tags_dropped = Signal(list, list)
    column_toggled = Signal(str, bool)
    """A column's key and whether the user made it visible (from the header menu)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDropIndicatorShown(False)  # the outline below shows the target instead
        self._drop_rows: list[int] = []
        self._outlines: list[QFrame] = []
        self.columns: list[ResultColumn] = []
        header = self.horizontalHeader()
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)

    # --- columns ---

    def set_columns(self, columns: Sequence[ResultColumn], hidden: Iterable[str]) -> None:
        """Record the model's columns and hide those in ``hidden`` (never the title)."""
        self.columns = list(columns)
        hide = set(hidden)
        for i, column in enumerate(self.columns):
            self.setColumnHidden(i, column.key != "title" and column.key in hide)

    def column_menu(self) -> QMenu:
        """A menu with a checkbox per column (the title can't be hidden)."""
        menu = QMenu(self)
        for i, column in enumerate(self.columns):
            action = menu.addAction(column.label)
            action.setCheckable(True)
            action.setChecked(not self.isColumnHidden(i))
            action.setEnabled(column.key != "title")
            action.toggled.connect(
                lambda visible, key=column.key: self.column_toggled.emit(key, visible)
            )
        return menu

    def _header_menu(self, point: QPoint) -> None:
        self.column_menu().exec(self.horizontalHeader().mapToGlobal(point))

    def drop_rows(self, point: QPoint) -> list[int]:
        """The rows a drop at ``point`` (viewport coordinates) would tag."""
        index = self.indexAt(point)
        if not index.isValid():
            return []
        selected = sorted({i.row() for i in self.selectionModel().selectedRows()})
        return selected if index.row() in selected else [index.row()]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if dragged_tags(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        rows = self.drop_rows(event.position().toPoint()) if dragged_tags(event.mimeData()) else []
        self._set_drop_rows(rows)
        if rows:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_drop_rows([])

    def dropEvent(self, event: QDropEvent) -> None:
        tags = dragged_tags(event.mimeData())
        rows = self.drop_rows(event.position().toPoint())
        self._set_drop_rows([])
        if not tags or not rows:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.tags_dropped.emit(rows, tags)

    def drop_outline(self) -> list[QRect]:
        """Where the drop target is outlined (viewport coordinates), for tests."""
        return [f.geometry() for f in self._outlines if not f.isHidden()]

    def _set_drop_rows(self, rows: list[int]) -> None:
        """Outline ``rows``: one frame per run of consecutive rows. Frames float over the
        viewport, so repainting cells (as selection changes do) can't break them up."""
        if rows == self._drop_rows:
            return
        self._drop_rows = rows
        runs: list[list[int]] = []
        for row in rows:
            if runs and row == runs[-1][-1] + 1:
                runs[-1].append(row)
            else:
                runs.append([row])
        while len(self._outlines) < len(runs):
            frame = QFrame(self.viewport())
            frame.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            frame.setObjectName("drop_outline")
            frame.setStyleSheet(f"#drop_outline {{ border: 2px dashed {OUTLINE_COLOR}; }}")
            self._outlines.append(frame)
        width = self.viewport().width()
        for i, frame in enumerate(self._outlines):
            if i >= len(runs):
                frame.hide()
                continue
            top = self.rowViewportPosition(runs[i][0])
            bottom = self.rowViewportPosition(runs[i][-1]) + self.rowHeight(runs[i][-1])
            frame.setGeometry(QRect(0, top, width, bottom - top))
            frame.show()
            frame.raise_()


def make_result_table(parent: QWidget | None = None) -> ResultTable:
    """A results table: whole-row multi-selection, read-only, fixed row heights, and a
    drop target for tags."""
    table = ResultTable(parent)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.verticalHeader().setVisible(False)
    # Fixed row heights and column widths: sizing to contents would read every row.
    table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
    table.verticalHeader().setDefaultSectionSize(table.fontMetrics().height() + 8)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(False)
    header.setMinimumSectionSize(40)
    table.setMinimumWidth(TITLE_MIN_WIDTH)
    return table


def set_column_widths(table: QTableView, columns: Sequence[ResultColumn]) -> None:
    """The title takes the remaining width; the other columns start narrow and can be
    resized. Call again whenever the columns change."""
    header = table.horizontalHeader()
    for i, column in enumerate(columns):
        if column.key == "title":
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
        else:
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            width = TAGS_WIDTH if column.key in (TAGS, KEYWORDS) else COLUMN_WIDTH
            table.setColumnWidth(i, NUMERIC_WIDTH if column.numeric else width)
