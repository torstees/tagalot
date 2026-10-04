"""The public file readers: EPUB package metadata and covers, and ComicInfo.xml (#296); PDF
info and covers, and Markdown front matter (#297)."""

import io
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from tagalot.themes.api import (
    epub_cover,
    pdf_cover,
    read_comic_info,
    read_epub,
    read_front_matter,
    read_pdf_info,
    split_keywords,
    split_people,
)
from tests.core.book_files import (
    comic_info,
    jpeg,
    write_cbz,
    write_epub,
    write_markdown,
    write_pdf,
)


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


# --- PDF (#297) ---


def test_pdf_info(tmp_path: Path) -> None:
    path = write_pdf(
        tmp_path / "a.pdf",
        pages=3,
        title="Good Omens",
        author="Terry Pratchett; Neil Gaiman",
        subject="An angel and a demon",
        keywords="Fantasy, Humor",
        creationDate=time.strptime("1990-05-01", "%Y-%m-%d"),
    )
    info = read_pdf_info(str(path))
    assert info["title"] == "Good Omens"
    assert info["authors"] == ["Terry Pratchett", "Neil Gaiman"]
    assert info["subject"] == "An angel and a demon"
    assert info["keywords"] == ["Fantasy", "Humor"]
    assert info["year"] == 1990
    assert info["pages"] == 3


def test_a_pdf_saying_nothing(tmp_path: Path) -> None:
    info = read_pdf_info(str(write_pdf(tmp_path / "a.pdf")))
    assert (info["title"], info["authors"], info["keywords"]) == (None, [], [])


def test_a_pdf_cover_is_its_first_page(tmp_path: Path) -> None:
    data = pdf_cover(str(write_pdf(tmp_path / "a.pdf", color=(200, 20, 20))), 120)
    assert data is not None
    image = Image.open(io.BytesIO(data))
    assert max(image.size) in range(110, 131)
    red, green, _ = image.convert("RGB").getpixel((image.width // 2, image.height // 2))  # type: ignore[misc]
    assert red > 150 > green


def test_a_broken_pdf_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4 nothing else")
    with pytest.raises(ValueError, match="PDF"):
        read_pdf_info(str(path))
    with pytest.raises(ValueError, match="PDF"):
        pdf_cover(str(path))


def test_pdfs_read_from_many_threads(tmp_path: Path) -> None:
    paths = [str(write_pdf(tmp_path / f"{n}.pdf", title=f"Book {n}")) for n in range(12)]
    with ThreadPoolExecutor(6) as pool:
        titles = list(pool.map(lambda p: read_pdf_info(p)["title"], paths))
        covers = list(pool.map(pdf_cover, paths))
    assert titles == [f"Book {n}" for n in range(12)]
    assert all(covers)


# --- names and keywords ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Terry Pratchett", ["Terry Pratchett"]),
        ("Pratchett, Terry", ["Pratchett, Terry"]),
        ("Terry Pratchett, Neil Gaiman", ["Terry Pratchett", "Neil Gaiman"]),
        ("Terry Pratchett and Neil Gaiman", ["Terry Pratchett", "Neil Gaiman"]),
        ("A. One & B. Two; C.  Three", ["A. One", "B. Two", "C. Three"]),
        (
            ["Ann Leckie", "Martha Wells; N. K. Jemisin"],
            ["Ann Leckie", "Martha Wells", "N. K. Jemisin"],
        ),
        (None, []),
    ],
)
def test_split_people(value: object, expected: list[str]) -> None:
    assert split_people(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("fantasy reading", ["fantasy", "reading"]),
        ("Science Fiction, Space Opera", ["Science Fiction", "Space Opera"]),
        (["#Genre/Fantasy", "to-read", "genre/fantasy"], ["Genre/Fantasy", "to-read"]),
        (1984, ["1984"]),
        (None, []),
    ],
)
def test_split_keywords(value: object, expected: list[str]) -> None:
    assert split_keywords(value) == expected


# --- Markdown front matter ---


def test_yaml_front_matter(tmp_path: Path) -> None:
    path = write_markdown(
        tmp_path / "a.md",
        "---\r\n"
        "Title: The Long Way\r\n"
        "authors: [Becky Chambers]\r\n"
        "series: Wayfarers\r\n"
        "number: 1\r\n"
        "universe: Galactic Commons\r\n"
        "tags: [space, found-family]\r\n"
        "keywords: Cozy\r\n"
        "source: Royal Road\r\n"
        "url: https://example.com/long-way\r\n"
        "date: 2014-07-29\r\n"
        "cover: images/cover.jpg\r\n"
        "rating: 5\r\n"
        "---\r\n"
        "# Chapter One\r\n",
    )
    found = read_front_matter(str(path))
    assert found["title"] == "The Long Way"
    assert found["authors"] == ["Becky Chambers"]
    assert (found["series"], found["series_index"]) == ("Wayfarers", 1.0)
    assert found["universe"] == "Galactic Commons"
    assert found["keywords"] == ["space", "found-family", "Cozy"]
    assert found["source"] == "Royal Road"
    assert found["link"] == "https://example.com/long-way"
    assert found["year"] == 2014
    assert found["cover"] == "images/cover.jpg"
    assert found["fields"]["rating"] == 5


def test_toml_front_matter(tmp_path: Path) -> None:
    path = write_markdown(
        tmp_path / "a.md",
        '+++\ntitle = "Gideon the Ninth"\nauthor = "Tamsyn Muir"\nseries_index = 1.5\n'
        'tags = ["necromancy"]\nyear = 2019\nurl = "not a web address"\n+++\nText\n',
    )
    found = read_front_matter(str(path))
    assert (found["title"], found["authors"]) == ("Gideon the Ninth", ["Tamsyn Muir"])
    assert (found["series_index"], found["keywords"], found["year"]) == (1.5, ["necromancy"], 2019)
    assert found["link"] is None


def test_no_front_matter_gives_the_heading(tmp_path: Path) -> None:
    found = read_front_matter(str(write_markdown(tmp_path / "a.md", "Intro\n\n#  A Story  #\n")))
    assert found["title"] == "A Story"
    assert found["fields"] == {}


def test_a_thematic_break_is_not_front_matter(tmp_path: Path) -> None:
    found = read_front_matter(str(write_markdown(tmp_path / "a.md", "---\n# Heading\n")))
    assert found["title"] == "Heading"


@pytest.mark.parametrize(
    "text", ["---\ntitle: [unclosed\n---\n", "---\n- a list\n---\n", "+++\nx = \n+++\n"]
)
def test_bad_front_matter_raises_value_error(tmp_path: Path, text: str) -> None:
    with pytest.raises(ValueError, match="front matter"):
        read_front_matter(str(write_markdown(tmp_path / "a.md", text)))
