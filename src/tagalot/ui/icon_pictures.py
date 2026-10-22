"""Tags' icons that are pictures (#507, DESIGN.md §7 *Tag icons*): drawn from the bytes the
tag tree carries, so nothing is read from disk.

Badges on items and the item page show an animated icon's first frame; tag trees play it
(:class:`~tagalot.ui.models.tag_tree.TagTreeModel`). A picture chosen as an icon is read in
a worker (:func:`read_icon_file`): an SVG is drawn to a PNG first, as core can't read one.
"""

import base64
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, QRectF, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter
from PySide6.QtSvg import QSvgRenderer

from tagalot.core.tag_pictures import ICON_SIZE, prepare_icon
from tagalot.core.tags import PictureData, TagError


@lru_cache(maxsize=512)
def icon_image(picture: PictureData) -> QImage:
    """The icon's first frame (a null image for bytes Qt can't read)."""
    image = QImage()
    image.loadFromData(picture.data)
    return image


@lru_cache(maxsize=256)
def is_animated(picture: PictureData) -> bool:
    """Whether the icon is an animated GIF (more than one frame)."""
    if not picture.data.startswith(b"GIF8"):
        return False
    buffer = QBuffer()
    buffer.setData(QByteArray(picture.data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    return QImageReader(buffer).imageCount() > 1


def fitted(rect: QRect, width: int, height: int) -> QRect:
    """A ``width`` by ``height`` picture scaled to fit in ``rect``, centered."""
    scale = min(rect.width() / max(width, 1), rect.height() / max(height, 1))
    target = QRect(0, 0, max(1, round(width * scale)), max(1, round(height * scale)))
    target.moveCenter(rect.center())
    return target


def paint_icon_picture(painter: QPainter, rect: QRect, image: QImage) -> None:
    """Draw an icon's frame in ``rect``, scaled to fit, smoothly."""
    if image.isNull():
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage(fitted(rect, image.width(), image.height()), image)
    painter.restore()


def icon_html(picture: PictureData, side: int) -> str:
    """An icon as an ``<img>`` for rich text, at most ``side`` pixels on its longer side (a
    GIF shows its first frame)."""
    scale = side / max(picture.width, picture.height, 1)
    width, height = max(1, round(picture.width * scale)), max(1, round(picture.height * scale))
    kind = "gif" if picture.data.startswith(b"GIF8") else "png"
    data = base64.b64encode(picture.data).decode("ascii")
    return (
        f'<img src="data:image/{kind};base64,{data}" width="{width}" height="{height}" '
        'style="vertical-align: middle">'
    )


SVG_SUFFIX = ".svg"


def read_icon_file(path: str) -> PictureData:
    """A picture file as a tag's icon (in a worker): an SVG drawn at the kept size, any
    other picture as core keeps it (:func:`~tagalot.core.tag_pictures.prepare_icon`)."""
    raw = Path(path).read_bytes()
    if Path(path).suffix.lower() == SVG_SUFFIX:
        raw = _svg_png(raw)
    return prepare_icon(raw)


def _svg_png(raw: bytes) -> bytes:
    renderer = QSvgRenderer(QByteArray(raw))
    if not renderer.isValid():
        raise TagError("That isn't an SVG picture Tagalot can read.")
    size = renderer.defaultSize()
    image = QImage(ICON_SIZE, ICON_SIZE, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        target = fitted(QRect(0, 0, ICON_SIZE, ICON_SIZE), size.width(), size.height())
        renderer.render(painter, QRectF(target))
    finally:
        painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")  # type: ignore[call-overload]  # the stubs want bytes; Qt a str
    return bytes(buffer.data().data())
