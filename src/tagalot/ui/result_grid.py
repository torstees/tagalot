"""The grid layout: search results as thumbnail cards (DESIGN.md §10, §12).

```
┌────────┐ ┌────────┐ ┌────────┐
│  img   │ │  img   │ │  icon  │
└────────┘ └────────┘ └────────┘
glacier    geyser     notes.txt
1.2 MB     980 KB               ← card lines (the theme's default, or the user's choice)
```

The grid shows the same model as the list (one card per row) and shares its selection, so
switching layouts keeps what is selected. It lays cards out in wrapping rows of one uniform
size, which stays fast for any number of results; thumbnails come from a
:class:`~tagalot.ui.thumbnails.ThumbnailLoader` as they are painted.
"""

from collections.abc import Mapping, Sequence

from PySide6.QtCore import (
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QHelpEvent,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolTip,
    QWidget,
)

from tagalot.ui.dnd import dragged_tags
from tagalot.ui.models.results import (
    MATCH,
    PLACEHOLDER,
    MatchValue,
    ResultsModel,
    display_value,
)
from tagalot.ui.result_table import OUTLINE_COLOR
from tagalot.ui.thumbnails import ThumbnailLoader, icon_for

PADDING = 6
"""Space around a card's thumbnail and text."""

CardLines = Mapping[str, Sequence[tuple[str, str]]]
"""``{type id: [(field name, label), …]}``: the lines under each type's card titles."""


class CardDelegate(QStyledItemDelegate):
    """Paints one card: the thumbnail (or its icon) above the title and card lines."""

    def __init__(self, grid: "ResultGrid") -> None:
        super().__init__(grid)
        self.grid = grid

    def card_size(self) -> QSize:
        metrics = self.grid.fontMetrics()
        lines = 1 + self.grid.max_card_lines() + int(self.grid.show_match)
        side = self.grid.thumbnail_size
        return QSize(side + 2 * PADDING, side + 2 * PADDING + 4 + lines * metrics.height())

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # type: ignore[override]
        return self.card_size()

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        painter.save()
        try:
            self._paint(painter, option, index)
        finally:
            painter.restore()

    def _paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        grid = self.grid
        style = grid.style()
        panel = QStyleOptionViewItem(option)
        self.initStyleOption(panel, index)
        panel.text = ""
        panel.icon = QIcon()  # the card paints its own
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, panel, painter, grid)

        rect: QRect = option.rect
        side = grid.thumbnail_size
        box = QRect(rect.x() + PADDING, rect.y() + PADDING, side, side)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        palette: QPalette = option.palette

        found = grid.results_model().row(index.row())
        title = PLACEHOLDER if found is None else found[0].title
        if found is not None and grid.loader is not None:
            thumb = grid.loader.get(found[0].id)
            if thumb is not None and thumb.image is not None:
                image = thumb.image
                target = image.size()
                if target.width() > side or target.height() > side:
                    target = target.scaled(box.size(), Qt.AspectRatioMode.KeepAspectRatio)
                x = box.x() + (side - target.width()) // 2
                y = box.y() + (side - target.height()) // 2
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.drawImage(QRect(x, y, target.width(), target.height()), image)
            elif thumb is not None:
                icon_side = max(16, side // 2)
                icon_rect = QRect(0, 0, icon_side, icon_side)
                icon_rect.moveCenter(box.center())
                icon_for(thumb.icon).paint(painter, icon_rect)
            else:
                self._placeholder(painter, box, palette)
        else:
            self._placeholder(painter, box, palette)

        metrics = grid.fontMetrics()
        width = rect.width() - 2 * PADDING
        y = box.bottom() + 4
        text_role = QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        painter.setPen(palette.color(text_role))
        line = QRect(rect.x() + PADDING, y, width, metrics.height())
        painter.drawText(
            line,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            metrics.elidedText(title, Qt.TextElideMode.ElideRight, width),
        )
        if found is not None:
            muted = palette.color(text_role if selected else QPalette.ColorRole.PlaceholderText)
            painter.setPen(muted)
            keys = [key for key, _label in grid.card_lines.get(found[0].type, ())]
            if grid.show_match:
                keys.append(MATCH)  # where its documents matched, last
            for key in keys:
                line.translate(0, metrics.height())
                value = found[1].get(key)
                if isinstance(value, MatchValue):
                    text = value.brief  # from the match on, so the card shows it
                else:
                    text = display_value(value, grid.displays.get(key))
                painter.drawText(
                    line,
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                    metrics.elidedText(text, Qt.TextElideMode.ElideRight, width),
                )

        if index.row() in grid.drop_rows:
            pen = QPen(QColor(OUTLINE_COLOR), 2, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(1, 1, -2, -2))

    def _placeholder(self, painter: QPainter, box: QRect, palette: QPalette) -> None:
        color = QColor(palette.color(QPalette.ColorRole.Mid))
        color.setAlpha(60)
        painter.fillRect(box.adjusted(8, 8, -8, -8), color)

    def helpEvent(
        self,
        event: QHelpEvent,
        view: QAbstractItemView,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        found = self.grid.results_model().row(index.row(), load=False)
        if found is None:
            return False
        lines = [found[0].title]
        for key, label in self.grid.card_lines.get(found[0].type, ()):
            value = display_value(found[1].get(key), self.grid.displays.get(key))
            if value:
                lines.append(f"{label}: {value}")
        match = found[1].get(MATCH)
        if self.grid.show_match and isinstance(match, MatchValue):
            lines.append(f"{match.text} ({match.where})")
        QToolTip.showText(event.globalPos(), "\n".join(lines), view)
        return True


class ResultGrid(QListView):
    """Search results as cards. Accepts tags dragged from the tagging panel, like the list.

    Signals: :attr:`tags_dropped` (rows, tag ids), :attr:`zoom_requested` (+1 bigger, -1
    smaller; from Ctrl+wheel), :attr:`menu_requested` (a global position and the row under
    it, or -1, for the page's
    context menu).
    """

    tags_dropped = Signal(list, list)
    zoom_requested = Signal(int)
    menu_requested = Signal(QPoint, int)

    def __init__(
        self,
        loader: ThumbnailLoader | None = None,
        thumbnail_size: int = 128,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.loader = loader
        self.thumbnail_size = thumbnail_size
        self.card_lines: dict[str, list[tuple[str, str]]] = {}
        self.show_match = False
        """Cards end with where their documents matched (a search inside documents)."""
        self.displays: dict[str, str] = {}
        """Display formats of the card lines' fields, by field name."""
        self.drop_rows: set[int] = set()
        self.setViewMode(QListView.ViewMode.ListMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setUniformItemSizes(True)
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(500)
        self.setMovement(QListView.Movement.Static)
        self.setSpacing(4)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDropIndicatorShown(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda point: self.menu_requested.emit(
                self.viewport().mapToGlobal(point), self.indexAt(point).row()
            )
        )
        self.delegate = CardDelegate(self)
        self.setItemDelegate(self.delegate)
        # Thumbnails arrive one by one; repaint at most every few frames.
        self._repaint = QTimer(self)
        self._repaint.setSingleShot(True)
        self._repaint.setInterval(30)
        self._repaint.timeout.connect(self.viewport().update)
        if loader is not None:
            loader.ready.connect(lambda _id: self._repaint.start())
        self._apply_size()

    def results_model(self) -> ResultsModel:
        model = self.model()
        assert isinstance(model, ResultsModel)
        return model

    # --- appearance ---

    def set_thumbnail_size(self, size: int) -> None:
        if size != self.thumbnail_size:
            self.thumbnail_size = size
            self._apply_size()

    def set_card_lines(self, lines: CardLines) -> None:
        """The lines under the titles, per type (``{type id: [(field, label), …]}``)."""
        self.card_lines = {t: list(v) for t, v in lines.items()}
        self._apply_size()

    def max_card_lines(self) -> int:
        return max((len(v) for v in self.card_lines.values()), default=0)

    def _apply_size(self) -> None:
        self.setGridSize(self.delegate.card_size() + QSize(4, 4))
        self.doItemsLayout()
        self.viewport().update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y()
            if steps:
                self.zoom_requested.emit(1 if steps > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    # --- dropping tags ---

    def drop_targets(self, point: QPoint) -> list[int]:
        """The rows a drop at ``point`` (viewport coordinates) would tag: the selection, if
        dropped on a selected card, else that card."""
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
        rows = (
            self.drop_targets(event.position().toPoint()) if dragged_tags(event.mimeData()) else []
        )
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
        rows = self.drop_targets(event.position().toPoint())
        self._set_drop_rows([])
        if not tags or not rows:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.tags_dropped.emit(rows, tags)

    def _set_drop_rows(self, rows: list[int]) -> None:
        if set(rows) != self.drop_rows:
            self.drop_rows = set(rows)
            self.viewport().update()


def grid_icon(kind: str, size: int = 16) -> QIcon:
    """A small drawn icon for the layout toggle: ``"list"`` (lines) or ``"grid"`` (squares),
    in the palette's text color, so it suits light and dark themes."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    color = QApplication.palette().color(QPalette.ColorRole.WindowText)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    if kind == "list":
        for y in (3, 7, 11):
            painter.drawRect(2, y, 12, 2)
    elif kind == "tree":
        painter.drawRect(2, 2, 10, 2)
        painter.drawRect(6, 7, 8, 2)
        painter.drawRect(6, 12, 8, 2)
        painter.drawRect(3, 4, 1, 10)
    else:
        for x in (2, 9):
            for y in (2, 9):
                painter.drawRect(x, y, 5, 5)
    painter.end()
    return QIcon(pixmap)
