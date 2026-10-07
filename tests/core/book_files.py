"""Tiny EPUBs, Kindle books, and comic archives for tests (the demo script has its own
copy)."""

import io
import struct
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
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


def write_pdf(
    path: Path, pages: int = 1, color: tuple[int, int, int] = (30, 120, 60), **info: Any
) -> Path:
    """A PDF of ``pages`` solid pages (Pillow writes it), with document info such as
    ``title``, ``author``, ``subject``, ``keywords``, and ``creationDate``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    info.setdefault("title", "")  # else Pillow names it after the file
    images = [Image.new("RGB", (60, 90), color) for _ in range(pages)]
    images[0].save(path, "PDF", save_all=True, append_images=images[1:], **info)
    return path


def write_markdown(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_docx(path: Path, core: str | None = None, thumbnail: bytes | None = None) -> Path:
    """A Word document's zip with ``core`` as ``docProps/core.xml`` (just the elements
    inside ``cp:coreProperties``) and an optional thumbnail."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as doc:
        doc.writestr("[Content_Types].xml", "<Types/>")
        doc.writestr("word/document.xml", "<document/>")
        if core is not None:
            doc.writestr(
                "docProps/core.xml",
                '<?xml version="1.0"?><cp:coreProperties'
                ' xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
                ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
                ' xmlns:dcterms="http://purl.org/dc/terms/">' + core + "</cp:coreProperties>",
            )
        if thumbnail is not None:
            doc.writestr("docProps/thumbnail.jpeg", thumbnail)
    return path


def write_odt(path: Path, meta: str, thumbnail: bytes | None = None) -> Path:
    """An OpenDocument text's zip with ``meta`` inside ``office:meta`` in ``meta.xml``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as doc:
        doc.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        doc.writestr(
            "meta.xml",
            '<?xml version="1.0"?><office:document-meta'
            ' xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
            ' xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<office:meta>" + meta + "</office:meta></office:document-meta>",
        )
        if thumbnail is not None:
            doc.writestr("Thumbnails/thumbnail.png", thumbnail)
    return path


def write_pages(path: Path, preview: bytes | None = None) -> Path:
    """A Pages file saved as one file: a zip of Apple's own data and a preview picture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as doc:
        doc.writestr("Index/Document.iwa", b"\x00apple")
        if preview is not None:
            doc.writestr("preview.jpg", preview)
    return path


def write_link(path: Path, url: str, name: str | None = None, kind: str = "Link") -> Path:
    """A link file for ``url`` in the format its extension names (``.url``, ``.webloc``,
    ``.desktop``)."""
    import plistlib

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".url":
        path.write_text(f"[InternetShortcut]\r\nURL={url}\r\nIconIndex=0\r\n", encoding="utf-8")
    elif path.suffix == ".webloc":
        path.write_bytes(plistlib.dumps({"URL": url}))
    else:
        lines = ["[Desktop Entry]", "Version=1.0", f"Type={kind}", f"URL={url}"]
        if name:
            lines.append(f"Name={name}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def text_pdf(lines: Sequence[str], info: Mapping[str, str] | None = None) -> bytes:
    """A one-page PDF whose text layer holds ``lines`` (Helvetica), with document info
    such as ``{"Title": …, "Author": …}``: real text, as a paper's first page has."""

    def literal(text: str) -> str:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return f"({escaped})"

    shown = " ".join(f"{literal(line)} Tj T*" for line in lines)
    stream = f"BT /F1 11 Tf 72 740 Td 14 TL {shown} ET".encode("latin-1")
    entries = " ".join(f"/{k} {literal(v)}" for k, v in (info or {}).items())
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        f"<< {entries} >>".encode("latin-1"),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def write_text_pdf(path: Path, lines: Sequence[str], **info: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text_pdf(lines, info))
    return path


def paged_pdf(pages: Sequence[Sequence[str]]) -> bytes:
    """A PDF with a text layer on each page: ``pages`` holds each page's lines."""

    def literal(text: str) -> str:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return f"({escaped})"

    count = len(pages)
    kids = " ".join(f"{3 + 2 * n} 0 R" for n in range(count))
    font = 3 + 2 * count
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
    ]
    for n, lines in enumerate(pages):
        shown = " ".join(f"{literal(line)} Tj T*" for line in lines)
        stream = f"BT /F1 11 Tf 72 740 Td 14 TL {shown} ET".encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font "
            f"<< /F1 {font} 0 R >> >> /Contents {4 + 2 * n} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


# --- Kindle and Mobipocket books ---

EXTH_TYPES = {
    "authors": 100,
    "publisher": 101,
    "description": 103,
    "isbn": 104,
    "subjects": 105,
    "date": 106,
    "asin": 113,
    "updated_title": 503,
    "language": 524,
}


def palmdoc_pack(data: bytes) -> bytes:
    """PalmDOC compression without back-references: literals, a space and a character in
    one byte, and runs of other bytes behind a count."""
    out = bytearray()
    i = 0
    while i < len(data):
        byte = data[i]
        if byte == 0x20 and i + 1 < len(data) and 0x40 <= data[i + 1] <= 0x7F:
            out.append(data[i + 1] ^ 0x80)
            i += 2
        elif byte == 0 or 0x09 <= byte <= 0x7F:
            out.append(byte)
            i += 1
        else:
            run = data[i : i + 8]
            out.append(len(run))
            out += run
            i += len(run)
    return bytes(out)


def write_mobi(
    path: Path,
    *,
    name: str = "A_Book",
    full_title: str | None = None,
    exth: Mapping[str, Any] | None = None,
    text: str = "",
    cover: bytes | None = None,
    images: Sequence[bytes] = (),
    encoding: int = 65001,
    compression: int = 2,
    encryption: int = 0,
    trailing: int = 0b11,
    record_size: int = 4096,
) -> Path:
    """A Mobipocket book: ``exth`` holds its details (keys of :data:`EXTH_TYPES`; a list
    gives a record each), ``text`` its HTML, cut into records of ``record_size`` bytes and
    compressed (``compression`` 1: none; 2: PalmDOC; 17480: HUFF/CDIC, written as is), each
    with the trailing entries ``trailing`` names (a cut character's bytes; an entry). The
    cover, when given, is the first picture after ``images``."""
    codec = "utf-8" if encoding == 65001 else "cp1252"
    raw = text.encode(codec)
    chunks = [raw[i : i + record_size] for i in range(0, len(raw), record_size)] or [b""]
    text_records = []
    for chunk in chunks:
        body = palmdoc_pack(chunk) if compression == 2 else chunk
        if trailing & 1:
            body += b"\x00"  # no character cut off: the entry is its one size byte
        for bit in range(1, 16):
            if trailing & (1 << bit):
                body += b"xy\x83"  # two bytes of something, then its size (3), backwards
        text_records.append(body)
    pictures = [*images, *([cover] if cover is not None else [])]
    first_image = 1 + len(text_records) if pictures else 0xFFFFFFFF

    records: list[tuple[int, bytes]] = []
    for key, value in (exth or {}).items():
        for item in value if isinstance(value, list | tuple) else [value]:
            records.append((EXTH_TYPES[key], str(item).encode(codec)))
    if cover is not None:
        records.append((201, struct.pack(">I", len(images))))
    exth_body = b"".join(struct.pack(">II", t, len(v) + 8) + v for t, v in records)
    exth_block = b"EXTH" + struct.pack(">II", 12 + len(exth_body), len(records)) + exth_body
    exth_block += b"\0" * (-len(exth_block) % 4)

    mobi_length = 0xE8
    title_bytes = (full_title or "").encode(codec)
    title_offset = 16 + mobi_length + len(exth_block)
    head = bytearray(16 + mobi_length)
    struct.pack_into(
        ">HHIHHH", head, 0, compression, 0, len(raw), len(text_records), 4096, encryption
    )
    head[16:20] = b"MOBI"
    struct.pack_into(">III", head, 20, mobi_length, 2, encoding)
    struct.pack_into(">I", head, 36, 6)
    struct.pack_into(">II", head, 84, title_offset, len(title_bytes))
    struct.pack_into(">I", head, 108, first_image)
    struct.pack_into(">I", head, 128, 0x40 if records else 0)
    struct.pack_into(">H", head, 242, trailing)
    record0 = bytes(head) + exth_block + title_bytes + b"\0\0"

    blobs = [record0, *text_records, *pictures]
    header = bytearray(78)
    header[: len(name)] = name.encode("latin-1")
    header[60:68] = b"BOOKMOBI"
    struct.pack_into(">H", header, 76, len(blobs))
    offset = 78 + 8 * len(blobs) + 2
    table = bytearray()
    for index, blob in enumerate(blobs):
        table += struct.pack(">IB", offset, 0) + (2 * index).to_bytes(3, "big")
        offset += len(blob)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + bytes(table) + b"\0\0" + b"".join(blobs))
    return path
