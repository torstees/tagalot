"""Reading a document's text as pages (#348): PDF pages, EPUB chapters in spine order, and
one page for Markdown, plain text, DOCX, and ODT."""

import zipfile
from pathlib import Path

import pytest

from tagalot.themes import document_text
from tagalot.themes.api import read_document_text
from tests.core.book_files import CONTAINER, paged_pdf, write_markdown


def test_a_pdf_page_by_page(tmp_path: Path) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(paged_pdf([["Attention is all", "you need"], [], ["Dropout (0.1)"]]))
    assert read_document_text(str(path)) == [
        "Attention is all you need",
        "",  # a page without text keeps its number
        "Dropout (0.1)",
    ]


def test_pdf_pages_are_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "long.pdf"
    path.write_bytes(paged_pdf([[f"Page {n}"] for n in range(1, 6)]))
    monkeypatch.setattr(document_text, "MAX_PDF_PAGES", 3)
    assert read_document_text(str(path)) == ["Page 1", "Page 2", "Page 3"]


def test_text_is_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "long.pdf"
    path.write_bytes(paged_pdf([["abcdef"], ["ghijkl"], ["mnopqr"]]))
    monkeypatch.setattr(document_text, "MAX_TEXT", 8)
    assert read_document_text(str(path)) == ["abcdef", "gh"]


def test_an_epub_chapter_by_chapter_in_reading_order(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata/>
  <manifest>
    <item id="two" href="Text/ch%202.xhtml" media-type="application/xhtml+xml"/>
    <item id="one" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
  </manifest>
  <spine><itemref idref="one"/><itemref idref="css"/><itemref idref="two"/></spine>
</package>"""
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("mimetype", "application/epub+zip")
        book.writestr("META-INF/container.xml", CONTAINER)
        book.writestr("OEBPS/content.opf", opf)
        book.writestr(
            "OEBPS/Text/ch1.xhtml",
            "<html><head><title>Chapter One</title><style>p {}</style></head>"
            "<body><h1>One</h1><p>The wizard&#8217;s staff</p>"
            "<script>var x = 1;</script></body></html>",
        )
        book.writestr("OEBPS/Text/ch 2.xhtml", "<html><body><p>Rincewind ran.</p></body></html>")
        book.writestr("OEBPS/style.css", "p { color: red }")
    assert read_document_text(str(path)) == [
        "One The wizard\N{RIGHT SINGLE QUOTATION MARK}s staff",
        "Rincewind ran.",
    ]


def test_markdown_without_its_front_matter_and_plain_text(tmp_path: Path) -> None:
    note = write_markdown(tmp_path / "note.md", "---\ntitle: A note\n---\n# Heading\n\nSome  text.")
    assert read_document_text(str(note)) == ["# Heading Some text."]
    plain = tmp_path / "readme.txt"
    plain.write_text("Line one\nLine two\n", encoding="utf-8")
    assert read_document_text(str(plain)) == ["Line one Line two"]


def test_docx_and_odt(tmp_path: Path) -> None:
    docx = tmp_path / "essay.docx"
    with zipfile.ZipFile(docx, "w") as doc:
        doc.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>First</w:t></w:r><w:r><w:t xml:space='preserve'> para"
            "</w:t></w:r></w:p><w:p><w:r><w:t>Second</w:t></w:r></w:p></w:body></w:document>",
        )
    assert read_document_text(str(docx)) == ["First para Second"]
    odt = tmp_path / "essay.odt"
    with zipfile.ZipFile(odt, "w") as doc:
        doc.writestr(
            "content.xml",
            '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:'
            'office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
            "<office:body><office:text><text:h>Title</text:h><text:p>Body "
            "<text:span>text</text:span></text:p></office:text></office:body>"
            "</office:document-content>",
        )
    assert read_document_text(str(odt)) == ["Title Body text"]


def test_other_files_and_broken_ones(tmp_path: Path) -> None:
    other = tmp_path / "comic.cbz"
    other.write_bytes(b"PK")
    assert read_document_text(str(other)) == []
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    with pytest.raises(ValueError, match="not a PDF"):
        read_document_text(str(broken))
    not_a_zip = tmp_path / "broken.epub"
    not_a_zip.write_bytes(b"nope")
    with pytest.raises(ValueError, match="not an EPUB"):
        read_document_text(str(not_a_zip))
