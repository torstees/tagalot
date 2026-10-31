"""The books theme's online details (#342): Open Library by ISBN, filling in only what a
book's files don't say, on the books demo. Answers come from ``tests/fixtures/online``;
nothing goes online."""

import importlib.util
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from sqlalchemy import select

from tagalot.builtin_themes.books import (
    OPEN_LIBRARY,
    SEARCH_FIELDS,
    Book,
    BooksTheme,
    first_picture,
    isbn_digits,
    matching_work,
    open_library_author,
    open_library_values,
    open_library_work,
    open_library_work_values,
)
from tagalot.core.ingest import IngestSession
from tagalot.core.models import Entity, FieldProvenance, FieldSource, OnlinePicture
from tagalot.core.online import Reply, consent_text
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.themes.api import EntityRef, OnlineRequest, Record
from tests.core.book_files import jpeg

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
FINAL_EMPIRE = (Path(__file__).resolve().parents[1] / "fixtures" / "online").joinpath(
    "openlibrary_final_empire.json"
)
FINAL_EMPIRE_URL = "https://openlibrary.org/isbn/9780765311788.json"
COLOUR_URL = "https://openlibrary.org/isbn/0552124753.json"


class OpenLibrary:
    """Open Library, answering from the fixture (404 for anything else)."""

    def __init__(self) -> None:
        corgi = {"publishers": ["Corgi"], "publish_date": "1990", "languages": []}
        self.answers = {
            FINAL_EMPIRE_URL: Reply(200, FINAL_EMPIRE.read_bytes()),
            COLOUR_URL: Reply(200, json.dumps(corgi).encode()),
        }
        self.urls: list[str] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Reply:
        self.urls.append(url)
        return self.answers.get(url, Reply(404, b'{"error": "notfound"}'))


@pytest.fixture
def library() -> OpenLibrary:
    return OpenLibrary()


@pytest.fixture
def session(tmp_path: Path, library: OpenLibrary) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_books_demo(tmp_path), Settings()) as session:
        session.online_opener = library
        session.set_online_lookups("allow")
        yield session


def _book(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(
            select(Entity.id).where(Entity.title == title, Entity.type == "books.book")
        )
    assert found is not None
    return found


def _fields(session: KeepSession, book: int) -> dict[str, Any]:
    table = session.schema.by_type_id("books.book").table
    with session.reader.connect() as conn:
        return dict(conn.execute(select(table).where(table.c.id == book)).one()._mapping)


def _books(session: KeepSession) -> list[int]:
    with session.reader.connect() as conn:
        return list(conn.scalars(select(Entity.id).where(Entity.type == "books.book")))


def test_what_is_looked_up() -> None:
    theme = BooksTheme()

    def book(isbn: str | None) -> Record:
        return Record(EntityRef(1, "books.book"), "A book", {"isbn": isbn}, {})

    assert list(theme.online_requests(Book, book("978-0-7653-1178-8"))) == [FINAL_EMPIRE_URL]
    assert list(theme.online_requests(Book, book("076531178x"))) == [
        "https://openlibrary.org/isbn/076531178X.json"
    ]
    assert list(theme.online_requests(Book, book("12345"))) == []
    assert list(theme.online_requests(Book, book(None))) == []
    assert isbn_digits("ISBN 0-552-12475-3") == "0552124753"


def test_reading_an_edition() -> None:
    edition = json.loads(FINAL_EMPIRE.read_text(encoding="utf-8"))
    values = open_library_values(edition)
    assert values["year"] == 2006
    assert values["publisher"] == "Tor"
    assert values["language"] == "en"
    assert values["description"].startswith("Experiencing an epiphany")
    described = {"description": {"type": "/type/text", "value": "Two\n lines."}}
    assert open_library_values(described)["description"] == "Two lines."
    assert open_library_values({"publish_date": "Oct 29, 2013"})["year"] == 2013
    assert open_library_values({"languages": [{"key": "/languages/xyz"}]})["language"] is None


def test_open_library_fills_in_only_what_the_files_dont_say(
    session: KeepSession, library: OpenLibrary
) -> None:
    colour = _book(session, "The Colour of Magic")
    session.writer.run(
        lambda conn: IngestSession(conn, session.schema).update(
            EntityRef(colour, "books.book"), year=1983
        )
    )  # as if its EPUB gave a year
    report = session.look_up(_books(session))
    assert sorted(library.urls) == [
        "https://openlibrary.org/isbn/0000000000.json",  # Equal Rites: not found
        COLOUR_URL,
        FINAL_EMPIRE_URL,
        "https://openlibrary.org/isbn/9780765316882.json",
    ]
    assert report.not_found == 2
    final_empire = _fields(session, _book(session, "The Final Empire"))
    assert (final_empire["year"], final_empire["publisher"], final_empire["language"]) == (
        2006,
        "Tor",
        "en",
    )
    assert final_empire["description"].startswith("Experiencing an epiphany")
    colour_fields = _fields(session, colour)
    assert (colour_fields["year"], colour_fields["publisher"]) == (1983, "Corgi")  # its own year
    with session.reader.connect() as conn:
        sources = dict(
            conn.execute(
                select(FieldProvenance.field, FieldProvenance.source).where(
                    FieldProvenance.entity_id == colour
                )
            ).all()
        )
    assert sources["publisher"] is FieldSource.FETCHED
    assert sources["year"] is FieldSource.EXTRACTED


# --- covers and authors' photos (#484) ---

TERRY = "https://openlibrary.org/authors/OL25712A.json"
COVER = "https://covers.openlibrary.org/b/id/8231-L.jpg"
PHOTO = "https://covers.openlibrary.org/a/id/6255-L.jpg"
ARCHIVE = "https://archive.org/download/olcovers0/olcovers0-L.zip/8231-L.jpg"
ARCHIVE_SERVER = "https://ia800401.us.archive.org/view_archive.php?file=8231-L.jpg"


def _corgi(**more: Any) -> Reply:
    edition = {"publishers": ["Corgi"], "publish_date": "1990", "languages": [], **more}
    return Reply(200, json.dumps(edition).encode())


def _author(session: KeepSession, name: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(
            select(Entity.id).where(Entity.title == name, Entity.type == "books.author")
        )
    assert found is not None
    return found


def _open_library_id(session: KeepSession, author: int) -> str | None:
    table = session.schema.by_type_id("books.author").table
    with session.reader.connect() as conn:
        return conn.scalar(select(table.c.open_library).where(table.c.id == author))


def _pictures(session: KeepSession) -> dict[int, str]:
    with session.reader.connect() as conn:
        rows = conn.execute(
            select(OnlinePicture.entity_id, OnlinePicture.url).where(
                OnlinePicture.data.is_not(None)
            )
        )
        return {e: u for e, u in rows}


def test_a_cover_and_the_authors_photo_in_one_run(
    session: KeepSession, library: OpenLibrary
) -> None:
    library.answers[COLOUR_URL] = _corgi(covers=[-1, 8231], authors=[{"key": "/authors/OL25712A"}])
    library.answers[TERRY] = Reply(200, b'{"name": "Terry Pratchett", "photos": [6255]}')
    # As Open Library does: the cover from the Internet Archive, two redirects on.
    library.answers[COVER] = Reply(302, b"", location=ARCHIVE)
    library.answers[ARCHIVE] = Reply(302, b"", location=ARCHIVE_SERVER)
    library.answers[ARCHIVE_SERVER] = library.answers[PHOTO] = Reply(200, jpeg())
    colour, terry = _book(session, "The Colour of Magic"), _author(session, "Terry Pratchett")
    report = session.look_up([colour])
    # His record in the same run; the cover by way of the Internet Archive.
    assert library.urls == [COLOUR_URL, TERRY, COVER, ARCHIVE, ARCHIVE_SERVER, PHOTO]
    assert _open_library_id(session, terry) == "OL25712A"
    assert _pictures(session) == {colour: COVER, terry: PHOTO}
    assert sorted(report.pictures) == sorted([colour, terry])


def test_a_record_with_another_name_isnt_the_authors(
    session: KeepSession, library: OpenLibrary
) -> None:
    library.answers[COLOUR_URL] = _corgi(authors=[{"key": "/authors/OL25712A"}])
    library.answers[TERRY] = Reply(200, b'{"name": "Someone Else", "photos": [6255]}')
    terry = _author(session, "Terry Pratchett")
    session.look_up([_book(session, "The Colour of Magic")])
    assert _open_library_id(session, terry) is None  # the guess was wrong: it goes
    assert PHOTO not in library.urls


def test_several_authors_cant_be_told_apart(session: KeepSession, library: OpenLibrary) -> None:
    """An edition naming two authors gives none of them an ID."""
    library.answers[COLOUR_URL] = _corgi(
        authors=[{"key": "/authors/OL25712A"}, {"key": "/authors/OL1A"}]
    )
    session.look_up([_book(session, "The Colour of Magic")])
    assert _open_library_id(session, _author(session, "Terry Pratchett")) is None


def test_open_library_author_ids() -> None:
    assert open_library_author("/authors/OL25712A") == "OL25712A"
    assert open_library_author("https://openlibrary.org/authors/ol25712a/Terry") == "OL25712A"
    assert open_library_author("Terry") is None
    assert first_picture([-1, 0, 6255]) == 6255
    assert first_picture(None) is None


# --- books found by title (#627) ---


def _search_url(title: str, author: str) -> str:
    query = urlencode({"title": title, "author": author, "fields": SEARCH_FIELDS, "limit": 20})
    return f"https://openlibrary.org/search.json?{query}"


EARTHSEA_SEARCH = _search_url("A Wizard of Earthsea", "Ursula K. Le Guin")
EARTHSEA_WORK = "https://openlibrary.org/works/OL100W.json"
EARTHSEA_COVER = "https://covers.openlibrary.org/b/id/7777-L.jpg"
LE_GUIN = "Ursula K. Le Guin"
WIZARD = "A Wizard of Earthsea"


def _doc(key: str, title: str, author: str, year: int | None, **more: Any) -> dict[str, Any]:
    """A work in a search's answer, shaped as Open Library gives them (made up here)."""
    found = {"key": f"/works/{key}", "title": title, "author_name": [author], **more}
    return found if year is None else {**found, "first_publish_year": year}


def _book_record(title: str, **fields: Any) -> Record:
    return Record(EntityRef(1, "books.book"), title, fields, {})


def test_a_book_with_no_isbn_is_searched_by_title() -> None:
    theme = BooksTheme()
    earthsea = _book_record(WIZARD, authors=f"{LE_GUIN}, Someone", year=1968)
    assert list(theme.online_requests(Book, earthsea)) == [
        OnlineRequest(EARTHSEA_SEARCH, by="title-year")  # the year is matched, not sent
    ]
    no_year = _book_record(WIZARD, authors=LE_GUIN)
    assert list(theme.online_requests(Book, no_year)) == [
        OnlineRequest(EARTHSEA_SEARCH, by="title")
    ]
    assert list(theme.online_requests(Book, _book_record("Untitled notes"))) == []  # no author
    found = _book_record(WIZARD, authors=LE_GUIN, open_library="OL100W")
    assert list(theme.online_requests(Book, found)) == [EARTHSEA_WORK]
    both = _book_record("X", isbn="0552124753", open_library="/works/OL100W")
    assert list(theme.online_requests(Book, both)) == [COLOUR_URL, EARTHSEA_WORK]
    assert open_library_work("https://openlibrary.org/works/ol100w/A_Wizard") == "OL100W"
    assert open_library_work("OL25712A") is None  # an author


def test_choosing_the_work() -> None:
    book = _book_record(WIZARD, authors=LE_GUIN, year=1968)
    right = _doc("OL100W", WIZARD, LE_GUIN, 1968)
    assert matching_work(book, [right]) == ("OL100W", 1968)
    # Another author's book of that title, another of hers, or no answer at all: no match.
    assert matching_work(book, [_doc("OL2W", WIZARD, "Someone", 1968)]) is None
    assert matching_work(book, [_doc("OL3W", "The Tombs of Atuan", LE_GUIN, 1970)]) is None
    assert matching_work(book, []) is None
    assert matching_work(book, {"not": "a list"}) is None
    # A year off by one is the book; two off, or none given, isn't.
    off_by_one = _doc("OL4W", WIZARD, LE_GUIN, 1969)
    assert matching_work(book, [off_by_one]) == ("OL4W", 1969)
    assert matching_work(book, [_doc("OL5W", WIZARD, LE_GUIN, 1970)]) is None
    assert matching_work(book, [_doc("OL6W", WIZARD, LE_GUIN, None)]) is None
    # The same year wins over one off; two equally good are a tie: nothing is guessed.
    assert matching_work(book, [off_by_one, right]) == ("OL100W", 1968)
    twin = _doc("OL7W", WIZARD, LE_GUIN, 1968)
    assert matching_work(book, [right, twin]) is None
    assert matching_work(book, [right, dict(right)]) == ("OL100W", 1968)  # one work, twice
    # Without a year, any year matches, so one work must be left.
    no_year = _book_record(WIZARD, authors=LE_GUIN)
    assert matching_work(no_year, [off_by_one]) == ("OL4W", 1969)
    assert matching_work(no_year, [off_by_one, right]) is None
    # Compared as works are keyed (case and punctuation aside); a subtitle may be the title's.
    keyed = _book_record("a wizard of earthsea!", authors="URSULA K LE GUIN", year=1968)
    assert matching_work(keyed, [right]) == ("OL100W", 1968)
    long = _book_record("Mistborn: The Final Empire", authors="Brandon Sanderson")
    titled = _doc("OL8W", "Mistborn", "Brandon Sanderson", 2006, subtitle="The Final Empire")
    assert matching_work(long, [titled]) == ("OL8W", 2006)


def test_reading_a_work() -> None:
    work = {
        "description": {"type": "/type/text", "value": "A boy\n mage."},
        "first_publish_date": "Sep 1968",
    }
    assert open_library_work_values(work) == {"year": 1968, "description": "A boy mage."}
    assert open_library_work_values({}) == {"year": None, "description": None}


def _earthsea_answers(library: OpenLibrary) -> None:
    docs = [
        _doc("OL200W", WIZARD, "Someone Else", 1968),
        _doc("OL100W", WIZARD, LE_GUIN, 1968),
        _doc("OL300W", "The Tombs of Atuan", LE_GUIN, 1970),
    ]
    library.answers[EARTHSEA_SEARCH] = Reply(200, json.dumps({"docs": docs}).encode())
    work = {"description": "A boy becomes a mage.", "covers": [-1, 7777]}
    library.answers[EARTHSEA_WORK] = Reply(200, json.dumps(work).encode())
    library.answers[EARTHSEA_COVER] = Reply(200, jpeg())


def test_a_book_found_by_title_gets_its_work_and_cover(
    session: KeepSession, library: OpenLibrary
) -> None:
    _earthsea_answers(library)
    session.set_online_find_by("title-year")
    earthsea = _book(session, WIZARD)
    report = session.look_up([earthsea])
    # The search, then the work by the ID it gave, in the same run; then its cover.
    assert library.urls == [EARTHSEA_SEARCH, EARTHSEA_WORK, EARTHSEA_COVER]
    fields = _fields(session, earthsea)
    assert fields["open_library"] == "OL100W"
    assert fields["description"] == "A boy becomes a mage."
    assert fields["year"] == 1968  # its own
    assert _pictures(session) == {earthsea: EARTHSEA_COVER}
    assert report.pictures == [earthsea]
    library.urls.clear()
    session.look_up([earthsea])  # from now on, straight to its work (kept, so not fetched)
    assert library.urls == []


def test_titles_are_sent_only_when_the_keep_allows_it(
    session: KeepSession, library: OpenLibrary
) -> None:
    _earthsea_answers(library)
    earthsea, hedge = _book(session, WIZARD), _book(session, "The Hedge Knight")
    session.look_up([earthsea, hedge])  # IDs only, the default
    assert library.urls == []
    assert _fields(session, earthsea)["open_library"] is None
    session.set_online_find_by("title-year")
    session.look_up([hedge])  # no year: not by title and year
    assert library.urls == []
    session.set_online_find_by("title")
    session.look_up([hedge])
    assert library.urls == [_search_url("The Hedge Knight", "George R. R. Martin")]


def test_what_open_library_is_sent() -> None:
    assert consent_text([OPEN_LIBRARY]) == (
        "Tagalot would send ISBNs and Open Library IDs to Open Library. "
        "Nothing else about your files leaves this computer."
    )
    by_year = consent_text([OPEN_LIBRARY], "title-year")
    assert "IDs, or titles with their authors and years, to" in by_year
    assert "IDs, or titles with their authors, to" in consent_text([OPEN_LIBRARY], "title")
