"""A rating as five stars on an item's page, set by clicking (DESIGN.md §12 "Detail page",
#419).

```
Rating:  ★ ★ ★ ◐ ☆  • edited      (click a star; its left half sets the half star)
```

The widget works in half stars, 0 to 10 (:func:`tagalot.core.formats.half_stars`), whatever
the rating is out of; :class:`EditableValue` turns them into the field's numbers.
"""

import math

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPalette
from PySide6.QtWidgets import QSizePolicy, QWidget

from tagalot.core.formats import STARS
from tagalot.ui.colors import STAR, color

STAR_SIZE = 18
"""Each star's width and height, in pixels."""
STAR_GAP = 3


def star_path(rect: QRectF) -> QPainterPath:
    """A five-pointed star filling ``rect``."""
    center = rect.center()
    outer = min(rect.width(), rect.height()) / 2
    inner = outer * 0.45
    path = QPainterPath()
    for i in range(10):
        radius = outer if i % 2 == 0 else inner
        angle = -math.pi / 2 + i * math.pi / 5
        point = QPointF(
            center.x() + radius * math.cos(angle), center.y() + radius * math.sin(angle)
        )
        if i == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    path.closeSubpath()
    return path


class StarRating(QWidget):
    """Five stars showing ``halves`` (0 to 10) half stars. When ``editable``, clicking a star
    emits :attr:`chosen` with the half stars it stands for: the whole star, or the half star
    for a click on its left half."""

    chosen = Signal(int)

    def __init__(self, halves: int = 0, *, editable: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.halves = halves
        self.editable = editable
        self._hover: int | None = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if editable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_halves(self, halves: int) -> None:
        self.halves = halves
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(STARS * STAR_SIZE + (STARS - 1) * STAR_GAP, STAR_SIZE + 2)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def star_rect(self, index: int) -> QRectF:
        """Where star ``index`` (0 to 4) is drawn."""
        return QRectF(index * (STAR_SIZE + STAR_GAP), 1, STAR_SIZE, STAR_SIZE)

    def halves_at(self, x: float) -> int | None:
        """The half stars a click at ``x`` stands for, or ``None`` between the stars' ends."""
        for i in range(STARS):
            rect = self.star_rect(i)
            if rect.left() - STAR_GAP / 2 <= x < rect.right() + STAR_GAP / 2:
                return i * 2 + (1 if x < rect.center().x() else 2)
        return None

    # --- painting ---

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shown = self._hover if self._hover is not None else self.halves
        outline = self.palette().color(QPalette.ColorRole.PlaceholderText)
        gold = color(STAR, self.palette())  # reads on light and dark colors (#558)
        fill = gold if self._hover is None else gold.lighter(115)
        for i in range(STARS):
            rect = self.star_rect(i)
            path = star_path(rect)
            filled = min(2, max(0, shown - i * 2))  # 0, 1 (left half), or 2
            if filled:
                painter.save()
                width = rect.width() if filled == 2 else rect.width() / 2
                painter.setClipRect(QRectF(rect.left(), rect.top(), width, rect.height()))
                painter.fillPath(path, fill)
                painter.restore()
            painter.setPen(fill.darker(130) if filled else outline)
            painter.drawPath(path)
        painter.end()

    # --- clicking ---

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.editable:
            hover = self.halves_at(event.position().x())
            if hover != self._hover:
                self._hover = hover
                self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.editable or event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        halves = self.halves_at(event.position().x())
        if halves is not None:
            self.chosen.emit(halves)
