"""Painting title cards (DESIGN.md §10 "Title cards", #415).

The core decides what a card says and its colors (:mod:`tagalot.core.thumbnails.title_card`);
this paints it into an image of the thumbnail's size, so it shows wherever thumbnails do.
It paints on a ``QImage``, which is safe in the thumbnail workers.
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPen,
    QTextLayout,
    QTextOption,
)

from tagalot.core.thumbnails.title_card import Card, Color
from tagalot.ui.fonts import place_font

MAX_TITLE_LINES = 5
MAX_SUBTITLE_LINES = 2


def paint_title_card(card: Card, size: int) -> QImage:
    """``card`` as an image ``size`` pixels on its longer side."""
    if card.aspect <= 1:
        width, height = max(round(size * card.aspect), 1), size
    else:
        width, height = size, max(round(size / card.aspect), 1)
    image = QImage(width, height, QImage.Format.Format_RGB32)
    background, ink = _color(card.background), _color(card.ink)
    image.fill(background)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        _paint(painter, card, width, height, background, ink)
    finally:
        painter.end()
    return image


def _paint(
    painter: QPainter, card: Card, width: int, height: int, background: QColor, ink: QColor
) -> None:
    unit = min(width, height)
    line = max(unit / 128, 1.0)
    frame = unit * 0.035
    border = _mix(background, ink, 0.25)
    painter.setPen(QPen(border, line))
    painter.drawRect(QRectF(frame, frame, width - 2 * frame, height - 2 * frame))

    margin = unit * 0.1
    inner = width - 2 * margin
    room = height - 2 * margin

    sub_font = place_font("titles", base=QFont())  # the keep's titles font (#565)
    sub_font.setPixelSize(max(round(unit * 0.075), 6))
    subtitle = _fit_lines(card.subtitle, sub_font, inner, MAX_SUBTITLE_LINES)
    sub_spacing = QFontMetricsF(sub_font).lineSpacing()
    rule_gap = sub_spacing * 0.8 if subtitle else 0.0
    sub_height = len(subtitle) * sub_spacing + 2 * rule_gap

    title_font, title = _fit_title(card.title, unit, inner, room - sub_height)
    title_spacing = QFontMetricsF(title_font).lineSpacing()
    total = len(title) * title_spacing + sub_height
    y = margin + max((room - total) / 2, 0)

    painter.setPen(ink)
    painter.setFont(title_font)
    for text in title:
        painter.drawText(
            QRectF(margin, y, inner, title_spacing), Qt.AlignmentFlag.AlignCenter, text
        )
        y += title_spacing
    if not subtitle:
        return
    y += rule_gap
    rule = unit * 0.12
    painter.setPen(QPen(border, line))
    painter.drawLine(QPointF(width / 2 - rule, y), QPointF(width / 2 + rule, y))
    y += rule_gap
    painter.setPen(_mix(ink, background, 0.2))
    painter.setFont(sub_font)
    for text in subtitle:
        painter.drawText(QRectF(margin, y, inner, sub_spacing), Qt.AlignmentFlag.AlignCenter, text)
        y += sub_spacing


def _fit_title(title: str, unit: int, width: float, room: float) -> tuple[QFont, list[str]]:
    """The largest bold font the title fits in whole words at, and its lines; at the
    smallest, words are broken and what doesn't fit is cut with an ellipsis."""
    font = place_font("titles", bold=True, base=QFont())
    smallest = max(round(unit * 0.07), 6)
    for pixels in range(max(round(unit * 0.2), smallest), smallest - 1, -1):
        font.setPixelSize(pixels)
        lines, whole = _lines(title, font, width, anywhere=False)
        spacing = QFontMetricsF(font).lineSpacing()
        if whole and len(lines) <= MAX_TITLE_LINES and len(lines) * spacing <= room:
            return QFont(font), lines
    font.setPixelSize(smallest)
    most = max(min(MAX_TITLE_LINES, int(room // QFontMetricsF(font).lineSpacing())), 1)
    return font, _fit_lines(title, font, width, most)


def _fit_lines(text: str, font: QFont, width: float, most: int) -> list[str]:
    """``text`` wrapped (breaking words if it must) in at most ``most`` lines, the last cut
    with an ellipsis when more is left."""
    if not text:
        return []
    lines, _ = _lines(text, font, width, anywhere=True)
    if len(lines) <= most:
        return lines
    rest = " ".join(lines[most - 1 :])
    last = QFontMetricsF(font).elidedText(rest, Qt.TextElideMode.ElideRight, width)
    return [*lines[: most - 1], last]


def _lines(text: str, font: QFont, width: float, anywhere: bool) -> tuple[list[str], bool]:
    """``text`` laid out in lines ``width`` wide, and whether every line fits (a word longer
    than a line doesn't, unless ``anywhere`` lets it break)."""
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(
        QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
        if anywhere
        else QTextOption.WrapMode.WordWrap
    )
    layout.setTextOption(option)
    utf16 = text.encode("utf-16-le")  # the layout counts UTF-16 code units
    lines: list[str] = []
    fits = True
    layout.beginLayout()
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        start, length = line.textStart(), line.textLength()
        part = utf16[2 * start : 2 * (start + length)].decode("utf-16-le", errors="ignore")
        lines.append(part.strip())
        fits = fits and line.naturalTextWidth() <= width + 0.5
    layout.endLayout()
    return [line for line in lines if line], fits


def _color(color: Color) -> QColor:
    return QColor(*color)


def _mix(a: QColor, b: QColor, amount: float) -> QColor:
    return QColor(
        round(a.red() + (b.red() - a.red()) * amount),
        round(a.green() + (b.green() - a.green()) * amount),
        round(a.blue() + (b.blue() - a.blue()) * amount),
    )
