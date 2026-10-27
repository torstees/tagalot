"""Results lists show the item under the mouse with an outline, not a fill (#473).

Qt's Windows 11 style tints the hovered row almost like a selected one, so after a click and
the arrow keys the clicked row, still under the mouse, looked selected too. The lists' rows
are drawn without the style's hover (:class:`NoHoverFill`), and :class:`HoverOutline` lays a
thin frame in the selection color over the row (or card) under the mouse instead: a cue
apart from the selection, and from the amber dashed outline of a tag drop.
"""

from PySide6.QtCore import QEvent, QModelIndex, QObject, QPoint, QRect, Qt
from PySide6.QtGui import QMouseEvent, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QListView,
    QProxyStyle,
    QStyle,
    QStyleOption,
    QStyleOptionViewItem,
    QWidget,
)

from tagalot.ui.selection import with_selected_text

_ITEM_PRIMITIVES = (
    QStyle.PrimitiveElement.PE_PanelItemViewItem,
    QStyle.PrimitiveElement.PE_PanelItemViewRow,
)


class NoHoverFill(QProxyStyle):
    """The application's style, drawing item views' rows and cells without their hover
    state. Only the results lists use it (:func:`outline_hover`)."""

    def drawPrimitive(
        self,
        element: QStyle.PrimitiveElement,
        option: QStyleOption,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        if element in _ITEM_PRIMITIVES:
            option = _without_hover(option)
        super().drawPrimitive(element, option, painter, widget)

    def drawControl(
        self,
        element: QStyle.ControlElement,
        option: QStyleOption,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        if element == QStyle.ControlElement.CE_ItemViewItem:
            option = _without_hover(option)
            if isinstance(option, QStyleOptionViewItem) and (
                option.state & QStyle.StateFlag.State_Selected
            ):
                # Text that reads on the selection's fill (Windows 11 fills a
                # list with alternating rows with the accent, text kept dark).
                option = QStyleOptionViewItem(option)
                with_selected_text(option, widget)
        super().drawControl(element, option, painter, widget)


def _without_hover(option: QStyleOption) -> QStyleOption:
    if not option.state & QStyle.StateFlag.State_MouseOver:
        return option
    copy = type(option)(option)
    copy.state &= ~QStyle.StateFlag.State_MouseOver
    return copy


class HoverOutline(QObject):
    """The outline over the item under the mouse in ``view``: the whole row in a list or
    tree, the card in a grid. Hidden when the mouse leaves, and during a drag."""

    def __init__(self, view: QAbstractItemView) -> None:
        super().__init__(view)
        self.view = view
        self.frame = QFrame(view.viewport())
        self.frame.setObjectName("hover_outline")
        self.frame.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        color = view.palette().color(view.palette().ColorRole.Highlight).name()
        self.frame.setStyleSheet(
            f"#hover_outline {{ border: 1px solid {color}; border-radius: 3px; }}"
        )
        self.frame.hide()
        self.point: QPoint | None = None
        """Where the mouse is in the viewport, while it's there."""
        view.viewport().setMouseTracking(True)
        view.viewport().installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if isinstance(event, QMouseEvent) and kind == QEvent.Type.MouseMove:
            self.point = event.position().toPoint()
            self.show_at(self.point)
        elif kind in (QEvent.Type.Leave, QEvent.Type.DragEnter, QEvent.Type.Hide):
            self.point = None
            self.frame.hide()
        elif kind == QEvent.Type.Paint and self.point is not None:
            # The list scrolled or changed under a still mouse: follow what's there now.
            self.show_at(self.point)
        return False

    def show_at(self, point: QPoint) -> None:
        """Outline the item at ``point`` in the viewport, or hide the outline."""
        index = self.view.indexAt(point)
        rect = self.rect_for(index)
        if rect is None:
            self.frame.hide()
            return
        self.frame.setGeometry(rect)
        self.frame.show()
        self.frame.raise_()

    def rect_for(self, index: QModelIndex) -> QRect | None:
        if not index.isValid():
            return None
        rect = self.view.visualRect(index)
        if isinstance(self.view, QListView):
            return rect.adjusted(1, 1, -1, -1)  # a grid's card
        return QRect(0, rect.y(), self.view.viewport().width(), rect.height())  # the row


def outline_hover(view: QAbstractItemView) -> HoverOutline:
    """Show the item under the mouse in ``view`` with an outline, not the style's fill.

    The view owns its style (a widget doesn't own the style it's given): deleted after the
    view, never while the view could still use it."""
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    style = NoHoverFill(app.style().name())
    style.setParent(view)
    view.setStyle(style)
    return HoverOutline(view)
