"""Kindle and Mobipocket books (#370): details, cover, and text, from books built here."""

import struct
from pathlib import Path

import pytest

from tagalot.core.thumbnails.render import load_mobi_cover, renderer_for
from tagalot.themes.api import (
    DOCUMENT_EXTENSIONS,
    MOBI_EXTENSIONS,
    ResourceInfo,
    mobi_cover,
    read_document_text,
    read_mobi,
)
from tagalot.themes.mobi import mobi_text, palmdoc_unpack, without_trailing_entries
from tests.core.book_files import jpeg, palmdoc_pack, write_epub, write_mobi

DETAILS = {
    "authors": ["Noah Gift, Kennedy Behrman, and Alfredo Deza"],
    "publisher": "O'Reilly Media, Inc.",
    "description": "<p>Automate <b>everything</b> &amp; more.</p>",
    "isbn": "978-1-4920-5769-7",
    "subjects": ["Computers", "DevOps"],
    "date": "2019-12-12T00:00:00+00:00",
    "asin": "B082P97LDW",
    "language": "en",
}


def test_details(tmp_path: Path) -> None:
    path = write_mobi(
        tmp_path / "pythonfordevops.mobi", full_title="Python for DevOps", exth=DETAILS
    )
    assert read_mobi(str(path)) == {
        "title": "Python for DevOps",
        "authors": ["Noah Gift, Kennedy Behrman, and Alfredo Deza"],  # as written
        "publisher": "O'Reilly Media, Inc.",
        "description": "Automate everything & more.",
        "isbn": "9781492057697",
        "asin": "B082P97LDW",
        "subjects": ["Computers", "DevOps"],
        "year": 2019,
        "language": "en",
        "version": 6,
        "encrypted": False,
        "cover": None,
    }


def test_the_title_and_authors(tmp_path: Path) -> None:
    """The updated title, else the full title, else the database's name; one record per
    author; text in the book's encoding."""
    updated = write_mobi(
        tmp_path / "a.mobi",
        full_title="Old",
        exth={"updated_title": "New", "authors": ["A B", "C D"]},
    )
    assert read_mobi(str(updated))["title"] == "New"
    assert read_mobi(str(updated))["authors"] == ["A B", "C D"]
    named = write_mobi(tmp_path / "b.mobi", name="Effective_DevOps")
    assert read_mobi(str(named))["title"] == "Effective DevOps"
    latin = write_mobi(
        tmp_path / "c.mobi", full_title="Café", encoding=1252, exth={"authors": "Zoë"}
    )
    assert (read_mobi(str(latin))["title"], read_mobi(str(latin))["authors"]) == ("Café", ["Zoë"])
    assert read_mobi(str(latin))["isbn"] is None


def test_the_cover(tmp_path: Path) -> None:
    picture = jpeg((10, 200, 10))
    named = write_mobi(tmp_path / "a.mobi", images=[jpeg((200, 0, 0))], cover=picture)
    assert read_mobi(str(named))["cover"] == 3  # record 0, one text record, a picture
    assert mobi_cover(str(named)) == picture  # the one its details name, not the first
    first = write_mobi(tmp_path / "b.mobi", images=[picture])  # names none: its first
    assert mobi_cover(str(first)) == picture
    assert mobi_cover(str(write_mobi(tmp_path / "c.mobi"))) is None  # no pictures
    other = write_mobi(tmp_path / "d.mobi", images=[b"not a picture"])
    assert mobi_cover(str(other)) is None
    image = load_mobi_cover(str(named), 32)
    assert image is not None
    assert image.getpixel((5, 5))[1] > 150  # type: ignore[index]


def test_text_as_pages(tmp_path: Path) -> None:
    """PalmDOC records (cut mid-character, with trailing entries) are unpacked, split at
    page breaks, and the markup taken out."""
    body = "<p>Ash falls on Luthadel.</p><mbp:pagebreak/><p>" + "Café " * 3000 + "</p>"
    path = write_mobi(tmp_path / "a.mobi", text=f"<html><body>{body}</body></html>")
    pages = mobi_text(str(path))
    assert pages[0] == "Ash falls on Luthadel."
    assert pages[1] == " ".join(["Café"] * 3000)  # across records, é cut between two
    assert read_document_text(str(path)) == pages
    plain = write_mobi(tmp_path / "b.mobi", text="<p>one</p>", compression=1, trailing=0)
    assert mobi_text(str(plain)) == ["one"]


def test_text_that_isnt_read(tmp_path: Path) -> None:
    locked = write_mobi(tmp_path / "a.azw", full_title="Locked", text="<p>x</p>", encryption=2)
    assert read_mobi(str(locked))["encrypted"]
    assert read_mobi(str(locked))["title"] == "Locked"  # its details aren't protected
    with pytest.raises(ValueError, match="protected"):
        read_document_text(str(locked))
    huff = write_mobi(tmp_path / "b.azw3", text="<p>x</p>", compression=17480)
    with pytest.raises(ValueError, match="HUFF/CDIC"):
        mobi_text(str(huff))


def test_files_that_arent_books(tmp_path: Path) -> None:
    for path in (
        write_epub(tmp_path / "a.mobi", title="An EPUB"),
        tmp_path / "empty.mobi",
    ):
        path.touch()
        with pytest.raises(ValueError, match="not a Mobipocket book"):
            read_mobi(str(path))
    whole = write_mobi(tmp_path / "whole.mobi", exth=DETAILS, text="<p>words</p>").read_bytes()
    for cut in (90, 200, 400, len(whole) - 5):
        (tmp_path / "cut.mobi").write_bytes(whole[:cut])
        _read_or_refuse(str(tmp_path / "cut.mobi"))


def _read_or_refuse(path: str) -> None:
    """A cut file reads (what's left of it) or raises ``ValueError``: never another error
    (an ``IndexError``, a ``struct.error``) that would mean a bad offset slipped through."""
    try:
        read_mobi(path)
        mobi_text(path)
    except ValueError:
        pass


def test_a_bad_details_block_is_left(tmp_path: Path) -> None:
    path = write_mobi(tmp_path / "a.mobi", full_title="Kept", exth={"authors": "A B"})
    data = bytearray(path.read_bytes())
    at = data.index(b"EXTH")
    struct.pack_into(">I", data, at + 8, 50)  # claims fifty records
    struct.pack_into(">I", data, at + 16, 9999)  # the first one runs past the block
    path.write_bytes(bytes(data))
    assert read_mobi(str(path))["title"] == "Kept"
    assert read_mobi(str(path))["authors"] == []


def test_palmdoc() -> None:
    assert palmdoc_unpack(b"abc\x80\x18") == b"abcabc"  # a copy: distance 3, length 3
    assert palmdoc_unpack(b"a\x80\x0f") == b"aaaaaaaaaaa"  # an overlapping copy
    assert palmdoc_unpack(b"\xe1\x02\x80\xff") == b" a\x80\xff"
    text = "Hi there, Zoë: <p>Ünïcode</p>".encode()
    assert palmdoc_unpack(palmdoc_pack(text)) == text
    with pytest.raises(ValueError, match="before its start"):
        palmdoc_unpack(b"\x80\x18")
    with pytest.raises(ValueError, match="middle of a copy"):
        palmdoc_unpack(b"ab\x80")


def test_trailing_entries() -> None:
    assert without_trailing_entries(b"text\x00xy\x83", 0b11) == b"text"
    # a character cut at the end: its byte, then how many such bytes there are
    assert without_trailing_entries(b"text\xa9\x01", 0b1) == b"text"
    # a 132-byte entry: its size in two bytes, the first (with the stop bit) the high one
    assert without_trailing_entries(b"text" + b"q" * 130 + b"\x81\x04", 0b10) == b"text"
    assert without_trailing_entries(b"text", 0) == b"text"
    with pytest.raises(ValueError, match="don't fit"):
        without_trailing_entries(b"\x8f", 0b10)  # an entry bigger than its record


def test_kindle_files_are_documents_with_covers() -> None:
    assert {".mobi", ".azw", ".azw3"} == MOBI_EXTENSIONS
    assert MOBI_EXTENSIONS <= DOCUMENT_EXTENSIONS
    for ext in MOBI_EXTENSIONS:
        renderer = renderer_for(ResourceInfo(1, "r", f"a{ext}", "file", ext, 1, 1, f"/a{ext}"))
        assert renderer is not None
        assert renderer.id == "mobi_cover"
