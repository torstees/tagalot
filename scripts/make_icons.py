"""Render Tagalot's artwork (the SVGs in src/tagalot/resources) to the files that use
it (#269, #327).

    uv run python scripts/make_icons.py

writes, from ``tagalot-shield-hash.svg`` (the app icon: it reads at 16 px):

- ``src/tagalot/resources/tagalot-<size>.png``: the window and taskbar icon (``ui.app``);
- ``packaging/tagalot.ico``: the Windows .exe and installer;
- ``packaging/tagalot.icns``: the macOS .app;
- ``packaging/tagalot.png``: the Linux AppImage (256 px);
- ``docs/images/favicon.ico`` and ``favicon.png``: the docs site's tab icon;

and copies the larger pictures to ``docs/images`` as SVGs with 256 px PNGs, for the docs
site and the README: the tower (``logo``), the knight with his visor up (``knight``, the
mascot), and the flag (``flag``).

Run it after changing an SVG, and commit the results: builds use them as they are.
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
DOCS_IMAGES = ROOT / "docs" / "images"
SVG = RESOURCES / "tagalot-shield-hash.svg"
"""The app icon."""
ICNS_SIZE = 1024
FAVICON_SIZES = (16, 32, 48)
PICTURES = {
    "logo": "tagalot-tower.svg",
    "knight": "tagalot-knight-visor-up.svg",
    "flag": "tagalot-flag.svg",
}
"""The docs site's and README's pictures: name in ``docs/images`` -> source SVG."""
PICTURE_SIZE = 256


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
    DOCS_IMAGES.mkdir(parents=True, exist_ok=True)
    favicons = [render(renderer, s) for s in FAVICON_SIZES]
    favicons[-1].save(
        DOCS_IMAGES / "favicon.ico",
        sizes=[(s, s) for s in FAVICON_SIZES],
        append_images=favicons[:-1],
    )
    favicons[1].save(DOCS_IMAGES / "favicon.png", optimize=True)
    for name, source in PICTURES.items():
        picture = QSvgRenderer(str(RESOURCES / source))
        if not picture.isValid():
            print(f"Can't read {source}")
            return 1
        render(picture, PICTURE_SIZE).save(DOCS_IMAGES / f"{name}.png", optimize=True)
        (DOCS_IMAGES / f"{name}.svg").write_bytes((RESOURCES / source).read_bytes())
    print(f"Wrote the favicon and {', '.join(PICTURES)} to docs/images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
