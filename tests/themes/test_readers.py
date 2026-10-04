"""The public file readers (#296): EPUB package metadata and covers, and ComicInfo.xml."""

import zipfile
from pathlib import Path

import pytest

from tagalot.themes.api import epub_cover, read_comic_info, read_epub
from tests.core.book_files import comic_info, jpeg, write_cbz, write_epub


def test_an_epub2_book(tmp_path: Path) -> None:
    cover = jpeg()
    path = write_epub(
        tmp_path / "b.epub",
        title="  The Colour   of Magic ",
        creators=[("Terry Pratchett", "aut"), ("Josh Kirby", "ill"), ("Someone", None)],
        subjects=["Fantasy", "Humor"],
        series=("Discworld", 1.0),
        meta=(
            "<dc:publisher>Corgi</dc:publisher><dc:language>en</dc:language>"
            "<dc:date>1983-11-24</dc:date>"
            '<dc:identifier opf:scheme="ISBN">978-0-552-12475-6</dc:identifier>'
            "<dc:description>&lt;p&gt;A &lt;b&gt;tourist&lt;/b&gt; &amp;amp; a wizard.&lt;/p&gt;"
            "</dc:description><dc:source>https://example.com/colour</dc:source>"
        ),
        cover=cover,
        cover_style="epub2",
    )
    book = read_epub(str(path))
    assert book["title"] == "The Colour of Magic"
    assert book["creators"] == [
        ("Terry Pratchett", "writer"),
        ("Josh Kirby", "artist"),
        ("Someone", None),
    ]
    assert (book["series"], book["series_index"]) == ("Discworld", 1.0)
    assert book["subjects"] == ["Fantasy", "Humor"]
    assert book["publisher"] == "Corgi"
    assert book["language"] == "en"
    assert book["year"] == 1983
    assert book["isbn"] == "9780552124756"
    assert book["description"] == "A tourist & a wizard."
    assert book["source"] == "https://example.com/colour"
    assert book["cover"] == "OEBPS/images/front.jpg"
    assert epub_cover(str(path)) == cover


def test_an_epub3_book(tmp_path: Path) -> None:
    path = write_epub(
        tmp_path / "b.epub",
        title="Mistborn",
        creators=[("Brandon Sanderson", None)],
        epub3_series=("Mistborn", 1),
        sets=["The Mistborn Trilogy"],
        meta='<meta refines="#c0" property="role" scheme="marc:relators">aut</meta>',
        cover=jpeg(),
    )
    book = read_epub(str(path))
    assert book["creators"] == [("Brandon Sanderson", "writer")]  # its role, refined
    assert (book["series"], book["series_index"]) == ("Mistborn", 1.0)
    assert book["collections"] == ["The Mistborn Trilogy"]
    assert book["cover"] == "OEBPS/images/front.jpg"


def test_an_epub_says_nothing(tmp_path: Path) -> None:
    book = read_epub(str(write_epub(tmp_path / "b.epub")))
    assert book["title"] is None
    assert book["creators"] == []
    assert book["cover"] is None
    assert epub_cover(str(tmp_path / "b.epub")) is None


@pytest.mark.parametrize("content", [b"not a zip", b"PK\x03\x04broken"])
def test_an_unreadable_epub_raises(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "b.epub"
    path.write_bytes(content)
    with pytest.raises((OSError, ValueError, zipfile.BadZipFile)):
        read_epub(str(path))


def test_an_epub_without_a_package_raises(tmp_path: Path) -> None:
    path = tmp_path / "b.epub"
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("mimetype", "application/epub+zip")
    with pytest.raises(ValueError, match="package"):
        read_epub(str(path))


def test_comic_info(tmp_path: Path) -> None:
    info = comic_info(
        Series="Saga",
        Number="12",
        Volume="2",
        Writer="Brian K. Vaughan",
        Penciller="Fiona Staples",
        Inker="Fiona Staples",
        CoverArtist="Fiona Staples, Someone Else",
        Genre="Science Fiction, Fantasy",
        Tags="Space",
        Web="https://example.com/saga-12",
        Year="2013",
        StoryArc="Chapter Two",
        SeriesGroup="Saga-verse",
        Publisher="Image",
    )
    comic = read_comic_info(str(write_cbz(tmp_path / "Saga 012.cbz", info)))
    assert comic is not None
    assert comic["series"] == "Saga"
    assert comic["number"] == "12"
    assert comic["volume"] == 2
    assert comic["writers"] == ["Brian K. Vaughan"]
    assert comic["artists"] == ["Fiona Staples", "Someone Else"]
    assert comic["genres"] == ["Science Fiction", "Fantasy"]
    assert comic["tags"] == ["Space"]
    assert comic["year"] == 2013
    assert comic["story_arc"] == "Chapter Two"
    assert comic["series_group"] == "Saga-verse"
    assert comic["web"] == "https://example.com/saga-12"


def test_a_comic_without_comic_info(tmp_path: Path) -> None:
    assert read_comic_info(str(write_cbz(tmp_path / "x.cbz"))) is None


def test_a_cbr_that_is_really_a_zip(tmp_path: Path) -> None:
    path = write_cbz(tmp_path / "x.cbr", comic_info(Series="Saga"))
    comic = read_comic_info(str(path))
    assert comic is not None
    assert comic["series"] == "Saga"


def test_comic_info_in_a_7z(tmp_path: Path) -> None:
    import py7zr

    path = tmp_path / "x.cb7"
    with py7zr.SevenZipFile(path, "w") as archive:
        archive.writestr(jpeg(), "001.jpg")
        archive.writestr(comic_info(Series="Saga", Number="3"), "ComicInfo.xml")
    comic = read_comic_info(str(path))
    assert comic is not None
    assert (comic["series"], comic["number"]) == ("Saga", "3")
