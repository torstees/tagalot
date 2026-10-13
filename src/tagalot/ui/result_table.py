"""The list layout's columns and table setup, shared by search pages and grouped sections."""

from collections.abc import Iterable, Sequence

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QPainter,
    QPalette,
    QResizeEvent,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHeaderView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QWidget,
)

from tagalot.core.search_fields import scope_fields, scoped_tables
from tagalot.core.theme_schema import ThemeSchema
from tagalot.ui.badges import BadgeDelegate
from tagalot.ui.dnd import dragged_tags
from tagalot.ui.models.results import (
    BADGES,
    KEYWORDS,
    MATCH,
    MATCH_HTML_ROLE,
    TAGS,
    ResultColumn,
)

TITLE_MIN_WIDTH = 200
TITLE_MAX_WIDTH = 480
"""The widest the title column starts: wider than that, the last column takes the rest
(#413). It fits the spare width until the user resizes it."""
TAGS_WIDTH = 220
DEFAULT_HIDDEN = frozenset({TAGS, KEYWORDS})
"""Columns hidden until the user shows them (right-click a column header)."""
OUTLINE_COLOR = "#f0a020"
"""The drop target's dashed outline: amber, distinct from the selection color."""
COLUMN_WIDTH = 140
MATCH_WIDTH = 360
NUMERIC_WIDTH = 100
BADGES_WIDTH = 90
DESCRIPTION_WIDTH = 320
"""About four badges, then "+n"; wider shows more (#398)."""


def list_columns(
    schema: ThemeSchema,
    types: Sequence[str],
    keywords: bool = False,
    *,
    contents: bool = False,
    badges: bool = True,
) -> list[ResultColumn]:
    """The list layout's columns for a scope: the title (named as the types call it), the
    type when several types are in scope, the item's tags (hidden unless the user shows
    them, :data:`DEFAULT_HIDDEN`; next to the title so they stay in view when shown), then
    every field the scoped types share that their pages show: card fields first, shown,
    then the types' description (#414), shown, then the rest, hidden until the user shows
    them (#314). The Badges column (#398) is shown unless ``badges`` is false (a keep whose
    tags have no icons), when it's offered hidden."""
    tables = scoped_tables(schema, types)
    described = {t.entity.description_field for t in tables} - {None}
    labels = {t.entity.title_label for t in tables}
    columns = [ResultColumn("title", labels.pop() if len(labels) == 1 else "Title")]
    if len(tables) != 1:
        columns.append(ResultColumn("type", "Type", sortable=False))
    if contents:  # the search looks inside documents: where each matched (#351)
        columns.append(ResultColumn(MATCH, "Match", sortable=False))
    columns.append(ResultColumn(BADGES, "Badges", sortable=False, hidden_by_default=not badges))
    columns.append(ResultColumn(TAGS, "Tags", sortable=False))
    if keywords:  # only in keeps whose files give keywords (§7)
        columns.append(ResultColumn(KEYWORDS, "Keywords", sortable=False))
    fields = [
        f
        for f in scope_fields(schema, types)
        if f.spec.card or f.spec.detail or f.name in described
    ]
    columns.extend(
        ResultColumn(
            f.name,
            f.spec.label,
            numeric=f.type in (int, float),
            display=f.spec.display,
            hidden_by_default=not (f.spec.card or f.name in described),
            description=f.name in described,
        )
        # card fields first, then the description
        for f in sorted(fields, key=lambda f: (not f.spec.card, f.name not in described))
    )
    return columns


def hidden_keys(
    columns: Iterable[ResultColumn], hidden: Iterable[str], shown: Iterable[str] = ()
) -> set[str]:
    """The keys of ``columns`` to hide: those the user hid, and those hidden by default
    that the user hasn't shown. Never the title."""
    hide, show = set(hidden), set(shown)
    return {
        c.key
        for c in columns
        if c.key != "title" and (c.key in hide or (c.hidden_by_default and c.key not in show))
    }


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
        self.start_widths: dict[int, int] = {}
        """Each column's starting width, by index (:func:`set_column_widths`)."""
        self.title_fitted = True
        """Whether the title still fits the spare width: until the user resizes it."""
        self._fitting = False
        header = self.horizontalHeader()
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)
        header.sectionResized.connect(self._section_resized)

    # --- columns ---

    def set_columns(
        self, columns: Sequence[ResultColumn], hidden: Iterable[str], shown: Iterable[str] = ()
    ) -> None:
        """Record the model's columns and hide those the user hid, and those hidden by
        default the user hasn't shown (:func:`hidden_keys`)."""
        self.columns = list(columns)
        hide = hidden_keys(self.columns, hidden, shown)
        for i, column in enumerate(self.columns):
            self.setColumnHidden(i, column.key in hide)
        self.fit_title()

    def _title(self) -> int | None:
        return next((i for i, c in enumerate(self.columns) if c.key == "title"), None)

    def fit_title(self) -> None:
        """Give the title the width the other shown columns leave, between
        :data:`TITLE_MIN_WIDTH` and :data:`TITLE_MAX_WIDTH`, unless the user resized it:
        on a narrow window every column fits, on a wide one the last column fills the rest
        (#413)."""
        title = self._title()
        if not self.title_fitted or title is None:
            return
        others = [i for i in range(len(self.columns)) if i != title and not self.isColumnHidden(i)]
        if not others:
            return  # the title alone: it fills the width as the last column
        spare = self.viewport().width() - sum(self.start_widths.get(i, 0) for i in others)
        self._fitting = True
        try:
            self.setColumnWidth(title, max(TITLE_MIN_WIDTH, min(TITLE_MAX_WIDTH, spare)))
        finally:
            self._fitting = False

    def _section_resized(self, index: int, _old: int, _new: int) -> None:
        if self._fitting or index != self._title():
            return
        header = self.horizontalHeader()
        shown = [
            header.visualIndex(i) for i in range(header.count()) if not header.isSectionHidden(i)
        ]
        if shown and header.visualIndex(index) == max(shown):
            return  # stretched as the last shown column, not resized by the user
        self.title_fitted = False

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.fit_title()

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
    header.setStretchLastSection(True)  # the last shown column fills the rest, as in the tree
    header.setMinimumSectionSize(40)
    header.setSectionsMovable(True)  # drag a heading to move its column, as in the tree
    table.setMinimumWidth(TITLE_MIN_WIDTH)
    return table


def set_column_widths(table: ResultTable, columns: Sequence[ResultColumn]) -> None:
    """Every column can be resized: the others start narrow, the title takes the spare
    width up to :data:`TITLE_MAX_WIDTH` (:meth:`ResultTable.fit_title`), and the last shown
    column fills the rest (#413). Call again whenever the columns change; then the title
    fits the width again."""
    header = table.horizontalHeader()
    table.start_widths = {}
    table.title_fitted = True
    for i, column in enumerate(columns):
        header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        if column.key == "title":
            continue  # fitted once the shown columns are known
        width = TAGS_WIDTH if column.key in (TAGS, KEYWORDS) else COLUMN_WIDTH
        if column.key == MATCH:
            width = MATCH_WIDTH
        elif column.key == BADGES:
            width = BADGES_WIDTH
        elif column.description:
            width = DESCRIPTION_WIDTH
        table.start_widths[i] = NUMERIC_WIDTH if column.numeric else width
        table.setColumnWidth(i, table.start_widths[i])
        delegate = (
            match_delegate(table)
            if column.key == MATCH
            else badge_delegate(table)
            if column.key == BADGES
            else None
        )
        table.setItemDelegateForColumn(i, delegate)  # type: ignore[arg-type]


class MatchDelegate(QStyledItemDelegate):
    """Draws the Match column's snippet on one line, the matched words in bold."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        rich = index.data(MATCH_HTML_ROLE)
        if not rich:
            super().paint(painter, option, index)
            return
        panel = QStyleOptionViewItem(option)
        self.initStyleOption(panel, index)
        panel.text = ""
        widget = panel.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, panel, painter, widget)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        role = QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        document = QTextDocument()
        document.setDefaultFont(option.font)
        document.setDocumentMargin(0)
        color = option.palette.color(role).name()
        document.setHtml(f'<span style="color: {color}; white-space: pre;">{rich}</span>')
        rect = option.rect.adjusted(4, 0, -4, 0)
        painter.save()
        painter.setClipRect(rect)
        top = rect.top() + (rect.height() - document.size().height()) / 2
        painter.translate(rect.left(), top)
        document.drawContents(painter)
        painter.restore()


def match_delegate(table: QTableView) -> MatchDelegate:
    """The table's Match column delegate (one per table, made when first needed)."""
    found = table.findChild(MatchDelegate)
    return found if found is not None else MatchDelegate(table)


def badge_delegate(table: QTableView) -> BadgeDelegate:
    """The table's Badges column delegate (one per table, made when first needed)."""
    found = table.findChild(BadgeDelegate)
    return found if found is not None else BadgeDelegate(table)
