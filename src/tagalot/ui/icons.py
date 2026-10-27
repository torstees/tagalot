"""Icons drawn from SVGs in the palette's colors (#556, DESIGN.md §12 *Icons*): the bundled
Lucide icons and themes' own SVGs (``core/icons.py`` says which file a name means).

An icon is drawn when it is painted, in the application palette's text color (its
highlighted-text color when selected, its disabled color when disabled), so it follows the
system's light and dark modes and a keep's own colors without being made again.
"""

import functools
import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, QRect, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from tagalot.core.icons import GENERIC, lucide_svg

logger = logging.getLogger(__name__)

KIND_ICONS = {
    "dir": "folder",
    "folder": "folder",
    "audio": "file-audio",
    "video": "file-video",
    "image": "file-image",
    "archive": "file-archive",
    "font": "type",
    "file": "file",
    "entity": "file",
}
"""The Lucide icon for each name a thumbnail chain's ``Icon`` may give (a file kind)."""


class _SvgEngine(QIconEngine):
    """Draws an SVG whose strokes are ``currentColor`` in the palette's color for the
    icon's mode, at any size."""

    def __init__(self, svg: bytes) -> None:
        super().__init__()
        self._svg = svg
        self._renderers: dict[str, QSvgRenderer] = {}
        self._pixmaps: dict[tuple[int, int, str], QPixmap] = {}

    def clone(self) -> QIconEngine:
        return _SvgEngine(self._svg)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        renderer = self._renderer(_color(mode))
        if renderer.isValid():
            side = min(rect.width(), rect.height())
            square = QRect(0, 0, side, side)
            square.moveCenter(rect.center())
            renderer.render(painter, square)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(
        self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float
    ) -> QPixmap:
        color = _color(mode)
        width, height = round(size.width() * scale), round(size.height() * scale)
        key = (width, height, color)
        pixmap = self._pixmaps.get(key)
        if pixmap is None:
            pixmap = QPixmap(max(width, 1), max(height, 1))
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self.paint(painter, QRect(0, 0, width, height), mode, state)
            painter.end()
            if len(self._pixmaps) > 32:
                self._pixmaps.clear()
            self._pixmaps[key] = pixmap
        pixmap = QPixmap(pixmap)
        pixmap.setDevicePixelRatio(scale)
        return pixmap

    def actualSize(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QSize:
        side = min(size.width(), size.height())
        return QSize(side, side)

    def isNull(self) -> bool:
        return not self._svg

    def _renderer(self, color: str) -> QSvgRenderer:
        renderer = self._renderers.get(color)
        if renderer is None:
            renderer = QSvgRenderer(QByteArray(self._svg.replace(b"currentColor", color.encode())))
            self._renderers[color] = renderer
        return renderer


def _color(mode: QIcon.Mode) -> str:
    palette = QApplication.palette()
    if mode == QIcon.Mode.Disabled:
        color = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
    elif mode == QIcon.Mode.Selected:
        color = palette.color(QPalette.ColorRole.HighlightedText)
    else:
        color = palette.color(QPalette.ColorRole.Text)
    return QColor(color).name()


@functools.cache
def _svg_icon(svg: bytes) -> QIcon:
    return QIcon(_SvgEngine(svg))


def lucide(name: str) -> QIcon:
    """One of the bundled Lucide icons; the generic icon for a name not bundled."""
    svg = lucide_svg(name)
    if svg is None:
        logger.debug("No Lucide icon %r: showing the generic icon", name)
        svg = lucide_svg(GENERIC) or b""
    return _svg_icon(svg)


def icon(source: str | None) -> QIcon:
    """The icon a source from ``core/icons.py`` means (a Lucide name, or an SVG file's
    path); the generic icon for None, or a file that can't be read."""
    if source is None:
        return lucide(GENERIC)
    if source.lower().endswith(".svg"):
        return _file_icon(source)
    return lucide(source)


@functools.cache
def _file_icon(path: str) -> QIcon:
    """A theme's own SVG, read once (a small file beside the theme's module)."""
    try:
        return _svg_icon(Path(path).read_bytes())
    except OSError as e:
        logger.warning("Can't read the icon %s: %s", path, e)
        return lucide(GENERIC)


def icon_for(name: str | None) -> QIcon:
    """The icon for a thumbnail's icon name: a file kind from a chain's ``Icon``
    (``"audio"``, ``"dir"``…), or a type's icon source; unknown names get the generic
    icon."""
    if name in KIND_ICONS:
        return lucide(KIND_ICONS[name])
    return icon(name)
