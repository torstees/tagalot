"""The color to draw a selected item's text and icons in (#556, DESIGN.md §12 *Icons*).

Styles draw a selection differently: Fusion fills it with the palette's highlight and
expects the highlighted-text color on it, while Qt's Windows 11 style tints it faintly and
keeps the text color, except in views with alternating row colors, which it fills with the
highlight while still drawing the text dark. Taking ``HighlightedText`` for granted put white
text on a faint tint in light mode, and the style's dark text on a strong accent fill.

So the selection's fill is measured: the style draws a selected item's panel into a small
picture, and the text gets whichever of the palette's text and highlighted-text colors
stands out more from it.
"""

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QProxyStyle,
    QStyle,
    QStyleOptionViewItem,
    QWidget,
)

_SIZE = QRect(0, 0, 24, 12)
_cache: dict[tuple[int, int, bool, bool], QColor] = {}


def selected_text_color(
    palette: QPalette, widget: QWidget | None = None, alternate: bool | None = None
) -> QColor:
    """The color for a selected item's text in ``widget`` (a view), with its ``palette``.

    ``alternate`` says whether the view alternates its rows' colors (read from ``widget``
    when it's an item view and this is None)."""
    style = widget.style() if widget is not None else QApplication.style()
    if alternate is None:
        alternate = bool(getattr(widget, "alternatingRowColors", lambda: False)())
    active = palette.currentColorGroup() != QPalette.ColorGroup.Inactive
    key = (id(style), palette.cacheKey(), alternate, active)
    color = _cache.get(key)
    if color is None:
        if len(_cache) > 64:
            _cache.clear()
        color = _measure(style, palette, widget, alternate)
        _cache[key] = color
    return color


def _measure(style: QStyle, palette: QPalette, widget: QWidget | None, alternate: bool) -> QColor:
    """Draw an empty selected item as the style does in ``widget``, and read its fill."""
    if isinstance(style, QProxyStyle) and style.baseStyle() is not None:
        style = style.baseStyle()  # not a proxy that asks for this color while drawing
    option = QStyleOptionViewItem()
    option.rect = _SIZE
    option.palette = palette
    option.state = (
        QStyle.StateFlag.State_Enabled
        | QStyle.StateFlag.State_Selected
        | QStyle.StateFlag.State_Active
    )
    if alternate:
        option.features |= QStyleOptionViewItem.ViewItemFeature.Alternate
    pixmap = QPixmap(_SIZE.size())
    pixmap.fill(palette.color(QPalette.ColorRole.Base))
    painter = QPainter(pixmap)
    style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, widget)
    painter.end()
    fill = pixmap.toImage().pixelColor(_SIZE.center())
    text = palette.color(QPalette.ColorRole.Text)
    highlighted = palette.color(QPalette.ColorRole.HighlightedText)
    return highlighted if contrast(highlighted, fill) > contrast(text, fill) else text


def contrast(a: QColor, b: QColor) -> float:
    """The WCAG contrast ratio of two colors (1 to 21)."""
    lighter, darker = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _luminance(color: QColor) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.redF())
        + 0.7152 * channel(color.greenF())
        + 0.0722 * channel(color.blueF())
    )


def with_selected_text(option: QStyleOptionViewItem, widget: QWidget | None) -> None:
    """Make a selected item's ``option`` draw its text in :func:`selected_text_color`,
    whichever color role the style reads it from."""
    if not option.state & QStyle.StateFlag.State_Selected:
        return
    color = selected_text_color(option.palette, widget)
    palette = QPalette(option.palette)
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.HighlightedText,
        QPalette.ColorRole.WindowText,
    ):
        palette.setColor(role, color)
    option.palette = palette
