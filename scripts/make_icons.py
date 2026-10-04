"""Render Tagalot's icon (src/tagalot/resources/tagalot.svg) to the files that use it (#269).

    uv run python scripts/make_icons.py

writes, from the SVG:

- ``src/tagalot/resources/tagalot-<size>.png``: the window and taskbar icon (``ui.app``);
- ``packaging/tagalot.ico``: the Windows .exe and installer;
- ``packaging/tagalot.icns``: the macOS .app;
- ``packaging/tagalot.png``: the Linux AppImage (256 px).

Run it after changing the SVG, and commit the results: builds use them as they are.
"""

import io
import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from tagalot.resources import ICON_SIZES

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ROOT / "src" / "tagalot" / "resources"
PACKAGING = ROOT / "packaging"
SVG = RESOURCES / "tagalot.svg"
ICNS_SIZE = 1024


def render(renderer: QSvgRenderer, size: int) -> Image.Image:
    """The SVG at ``size`` x ``size`` pixels, as a Pillow image with transparency."""
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return Image.open(io.BytesIO(bytes(data.data()))).convert("RGBA")


def main() -> int:
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    renderer = QSvgRenderer(str(SVG))
    if not renderer.isValid():
        print(f"Can't read {SVG}")
        return 1
    for size in ICON_SIZES:
        render(renderer, size).save(RESOURCES / f"tagalot-{size}.png", optimize=True)
    big = render(renderer, 256)
    big.save(PACKAGING / "tagalot.png", optimize=True)
    # Each ICO size drawn at that size (crisper than one image scaled down).
    smaller = [render(renderer, s) for s in ICON_SIZES[:-1]]
    big.save(PACKAGING / "tagalot.ico", sizes=[(s, s) for s in ICON_SIZES], append_images=smaller)
    render(renderer, ICNS_SIZE).save(PACKAGING / "tagalot.icns")
    print(f"Wrote {len(ICON_SIZES)} PNGs, tagalot.png, tagalot.ico, and tagalot.icns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
