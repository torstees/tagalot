"""Tag badges: the icons of an item's own tags, in a row of fixed-size squares (#398,
DESIGN.md §12).

As many badges as fit the space are drawn, then "+n" for the rest: across a grid card's
thumbnail (bottom-left, each on a translucent backing so it reads on any picture), or across
the Badges column. :func:`badges_that_fit` decides how many; the tooltip lists them all.
"""

from collections.abc import Callable, Sequence

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

GAP = 2
"""Space between badges."""


def badges_that_fit(count: int, width: int, side: int, more: Callable[[int], int]) -> int:
    """How many of ``count`` badges, each ``side`` wide with :data:`GAP` between them, fit
    in ``width``, leaving room for the "+n" of the rest (``more(n)`` is its width)."""
    if count * side + max(count - 1, 0) * GAP <= width:
        return count
    for shown in range(count - 1, -1, -1):
        used = shown * (side + GAP) + more(count - shown)
        if used <= width:
            return shown
    return 0


def badge_side(metrics: QFontMetrics) -> int:
    """A badge's size for a font: a line of text, with a little room around the icon."""
    return metrics.height() + 2


def paint_badges(
    painter: QPainter,
    rect: QRect,
    icons: Sequence[str],
    *,
    backing: QColor | None = None,
    text_color: QColor | None = None,
) -> int:
    """Draw ``icons`` left to right in ``rect`` (vertically centered), as many as fit, then
    "+n"; returns how many icons were drawn. With ``backing``, each badge (and the "+n")
    sits on a rounded square of that color."""
    if not icons:
        return 0
    metrics = painter.fontMetrics()
    side = min(badge_side(metrics), rect.height())

    def more_width(n: int) -> int:
        return metrics.horizontalAdvance(f"+{n}") + 6

    shown = badges_that_fit(len(icons), rect.width(), side, more_width)
    y = rect.y() + (rect.height() - side) // 2
    x = rect.x()
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        if text_color is not None:
            painter.setPen(text_color)
        center = Qt.AlignmentFlag.AlignCenter
        for icon in icons[:shown]:
            box = QRect(x, y, side, side)
            if backing is not None:
                painter.setBrush(backing)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(box, 4, 4)
                painter.setPen(text_color if text_color is not None else QColor("black"))
            painter.drawText(box, center, icon)
            x += side + GAP
        rest = len(icons) - shown
        if rest:
            box = QRect(x, y, more_width(rest), side)
            if backing is not None:
                painter.setBrush(backing)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(box, 4, 4)
                painter.setPen(text_color if text_color is not None else QColor("black"))
            painter.drawText(box, center, f"+{rest}")
    finally:
        painter.restore()
    return shown


class BadgeDelegate(QStyledItemDelegate):
    """Draws the Badges column: as many badges as fit the column's width, then "+n"."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        cell = QStyleOptionViewItem(option)
        self.initStyleOption(cell, index)
        cell.text = ""
        widget = option.widget
        style = widget.style() if widget is not None else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, cell, painter, widget)
        icons = str(index.data() or "").split()  # icons have no spaces (clean_icon)
        if not icons:
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        role = QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        rect = option.rect.adjusted(4, 1, -4, -1)
        painter.save()
        painter.setFont(option.font)
        paint_badges(painter, rect, icons, text_color=option.palette.color(role))
        painter.restore()
