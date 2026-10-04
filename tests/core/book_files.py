"""Tiny EPUBs and comic archives for tests (the demo script has its own copy)."""

import io
import zipfile
from collections.abc import Sequence
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def jpeg(color: tuple[int, int, int] = (200, 30, 30), size: int = 40) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, "JPEG")
    return buffer.getvalue()


def write_epub(
    path: Path,
    *,
    title: str | None = None,
    creators: Sequence[tuple[str, str | None]] = (),
    subjects: Sequence[str] = (),
    series: tuple[str, float] | None = None,
    epub3_series: tuple[str, float] | None = None,
    sets: Sequence[str] = (),
    meta: str = "",
    cover: bytes | None = None,
    cover_style: str = "epub3",
) -> Path:
    """An EPUB with this metadata (``creators`` as ``(name, role code)``); ``meta`` is more
    XML for the metadata block. ``cover_style`` ``"epub2"`` names the cover with
    ``<meta name="cover">``."""
    lines = []
    if title is not None:
        lines.append(f"<dc:title>{escape(title)}</dc:title>")
    for n, (name, code) in enumerate(creators):
        role = f' opf:role="{code}"' if code else ""
        lines.append(f'<dc:creator id="c{n}"{role}>{escape(name)}</dc:creator>')
    lines += [f"<dc:subject>{escape(s)}</dc:subject>" for s in subjects]
    if series:
        lines.append(f'<meta name="calibre:series" content="{escape(series[0])}"/>')
        lines.append(f'<meta name="calibre:series_index" content="{series[1]}"/>')
    if epub3_series:
        lines.append(f'<meta property="belongs-to-collection" id="s">{epub3_series[0]}</meta>')
        lines.append('<meta refines="#s" property="collection-type">series</meta>')
        lines.append(f'<meta refines="#s" property="group-position">{epub3_series[1]}</meta>')
    for n, name in enumerate(sets):
        lines.append(f'<meta property="belongs-to-collection" id="set{n}">{name}</meta>')
        lines.append(f'<meta refines="#set{n}" property="collection-type">set</meta>')
    items = '<item id="text" href="text.xhtml" media-type="application/xhtml+xml"/>'
    if cover is not None:
        if cover_style == "epub3":
            items += (
                '<item id="img" href="images/front.jpg" media-type="image/jpeg"'
                ' properties="cover-image"/>'
            )
        else:
            items += '<item id="img" href="images/front.jpg" media-type="image/jpeg"/>'
            lines.append('<meta name="cover" content="img"/>')
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">
    {"".join(lines)}{meta}
  </metadata>
  <manifest>{items}</manifest>
  <spine><itemref idref="text"/></spine>
</package>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        book.writestr("META-INF/container.xml", CONTAINER)
        book.writestr("OEBPS/content.opf", opf)
        book.writestr("OEBPS/text.xhtml", "<html><body><p>Once upon a time.</p></body></html>")
        if cover is not None:
            book.writestr("OEBPS/images/front.jpg", cover)
    return path


def comic_info(**fields: str) -> str:
    """A ``ComicInfo.xml`` with these elements (``Series="Saga"``)."""
    body = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in fields.items())
    return f'<?xml version="1.0"?><ComicInfo>{body}</ComicInfo>'


def write_cbz(path: Path, info: str | None = None, pages: int = 2) -> Path:
    """A comic archive of ``pages`` JPEG pages, with ``info`` as its ``ComicInfo.xml``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as comic:
        for n in range(1, pages + 1):
            comic.writestr(f"{n:03}.jpg", jpeg((20 * n, 90, 160)))
        if info is not None:
            comic.writestr("ComicInfo.xml", info)
    return path
