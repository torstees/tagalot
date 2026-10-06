"""Reading a document's text, as pages, for searching inside documents (DESIGN.md §8
*Search inside documents*).

:func:`read_document_text` is public, re-exported by :mod:`tagalot.themes.api`, so a theme
that reads some files its own way (``Theme.document_text``) can still use it for the rest.
Like the other readers it only reads, and raises ``OSError`` or ``ValueError`` for a file
that can't be read at all. A PDF's pages are read one at a time under :data:`PDFIUM`, so a
long document doesn't hold up other PDF readers (thumbnails, scans) for its whole length.
"""

import html
import posixpath
import re
import zipfile
from collections.abc import Iterable
from urllib.parse import unquote
from xml.etree import ElementTree

from tagalot.themes.readers import PDFIUM, _local, _opf_path, _xml

MAX_TEXT = 5 * 1024 * 1024
"""Characters of text kept from one file; the rest of a very long document isn't
searchable (the file isn't refused)."""
MAX_PDF_PAGES = 2000
"""Pages of a PDF read."""
MAX_PART = 20 * 1024 * 1024
"""A part of an EPUB or office document larger than this isn't read."""
DOCUMENT_EXTENSIONS = frozenset({".pdf", ".epub", ".md", ".markdown", ".txt", ".docx", ".odt"})
"""Files :func:`read_document_text` reads; others have no text to give."""

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_ODF_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_HTML_SKIP = re.compile(r"<(script|style|head)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def read_document_text(path: str) -> list[str]:
    """A document's text as pages, by its extension: a PDF's pages (PDFium's reading of
    them: a scanned PDF without a text layer has none), an EPUB's spine documents in reading
    order (its chapters), and one page for Markdown (without its front matter), plain text,
    DOCX, and ODT. Whitespace is collapsed to single spaces; empty pages stay, so page
    numbers hold. At most :data:`MAX_TEXT` characters in all and :data:`MAX_PDF_PAGES`
    pages. ``[]`` for a file of another kind (API version 6)."""
    ext = posixpath.splitext(path.replace("\\", "/"))[1].lower()
    if ext == ".pdf":
        pages: Iterable[str] = _pdf_pages(path)
    elif ext == ".epub":
        pages = _epub_pages(path)
    elif ext in (".md", ".markdown"):
        pages = [_without_front_matter(_read_text(path))]
    elif ext == ".txt":
        pages = [_read_text(path)]
    elif ext == ".docx":
        pages = [_docx_text(path)]
    elif ext == ".odt":
        pages = [_odt_text(path)]
    else:
        return []
    return _limited(pages)


def _limited(pages: Iterable[str]) -> list[str]:
    found: list[str] = []
    left = MAX_TEXT
    try:
        for page in pages:
            text = " ".join(page.split())
            found.append(text[:left])
            left -= len(found[-1])
            if left <= 0:
                break
    finally:
        close = getattr(pages, "close", None)
        if close is not None:  # a reader stopped early lets go of its file now
            close()
    return found


def _read_text(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read(MAX_TEXT * 4)  # UTF-8 takes at most four bytes a character
    return data.decode("utf-8", errors="replace")


def _without_front_matter(text: str) -> str:
    lines = text.split("\n")
    marker = lines[0].strip() if lines else ""
    if marker not in ("---", "+++"):
        return text
    ends = ("---", "...") if marker == "---" else ("+++",)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ends), None)
    return text if end is None else "\n".join(lines[end + 1 :])


def _pdf_pages(path: str) -> Iterable[str]:
    import pypdfium2

    with PDFIUM:
        try:
            pdf = pypdfium2.PdfDocument(path)
        except pypdfium2.PdfiumError as e:
            raise ValueError(f"not a PDF PDFium can open: {e}") from e
        count = min(len(pdf), MAX_PDF_PAGES)
    try:
        for index in range(count):
            with PDFIUM:  # a page at a time: other PDF readers aren't kept waiting
                page = pdf[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
            yield text
    finally:
        with PDFIUM:
            pdf.close()


def _html_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return html.unescape(_TAG.sub(" ", _HTML_SKIP.sub(" ", text)))


def _epub_pages(path: str) -> Iterable[str]:
    try:
        book = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ValueError(f"not an EPUB: {e}") from e
    with book:
        opf_path = _opf_path(book)
        package = _xml(book.read(opf_path))
        folder = posixpath.dirname(opf_path)
        manifest: dict[str, tuple[str, str]] = {}
        for item in package.iter():
            if _local(item.tag) == "item" and item.get("id") and item.get("href"):
                href = posixpath.normpath(posixpath.join(folder, unquote(str(item.get("href")))))
                manifest[str(item.get("id"))] = (href, item.get("media-type") or "")
        names = {info.filename: info for info in book.infolist()}
        for ref in package.iter():
            if _local(ref.tag) != "itemref":
                continue
            href, media = manifest.get(ref.get("idref") or "", ("", ""))
            info = names.get(href)
            if info is None or "html" not in media or info.file_size > MAX_PART:
                continue
            yield _html_text(book.read(info))


def _zip_xml(path: str, name: str) -> ElementTree.Element:
    try:
        doc = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a zip-based document: {e}") from e
    with doc:
        try:
            info = doc.getinfo(name)
        except KeyError:
            raise ValueError(f"no {name} inside") from None
        if info.file_size > MAX_PART:
            raise ValueError(f"{name} is too large")
        return ElementTree.fromstring(doc.read(info))


def _docx_text(path: str) -> str:
    body = _zip_xml(path, "word/document.xml")
    paragraphs = ("".join(t.text or "" for t in p.iter(f"{_W}t")) for p in body.iter(f"{_W}p"))
    return "\n".join(paragraphs)


def _odt_text(path: str) -> str:
    content = _zip_xml(path, "content.xml")
    blocks = (
        "".join(element.itertext())
        for element in content.iter()
        if element.tag in (f"{_ODF_TEXT}p", f"{_ODF_TEXT}h")
    )
    return "\n".join(blocks)
