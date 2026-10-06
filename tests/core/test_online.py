"""Online lookups in the core (#339, DESIGN.md §9 *Online details*): only declared https
hosts, rate-limited fetching that backs off, answers kept in the keep, the theme applying
them with provenance ``fetched``, and nothing at all without the keep's consent."""

import io
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from http.client import HTTPMessage
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.fields import edit_field
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import KeepConfigError, ThemeRef, create_keep, load_keep_config
from tagalot.core.models import CachedResponse, FieldProvenance, FieldSource
from tagalot.core.online import (
    Fetcher,
    LookupReport,
    NotAllowedError,
    OnlineError,
    Reply,
    UnavailableError,
    _SameHostRedirects,
    consent_text,
    look_up,
    source_for,
)
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import (
    Entity,
    EntityRef,
    IngestContext,
    OnlineResponse,
    OnlineSource,
    Record,
    Theme,
    ThemeDeclarationError,
    field,
)
from tagalot.themes.loader import validate_theme

LIBRARY = OnlineSource("Open Library", "openlibrary.org", "ISBNs")
ARXIV = OnlineSource("arXiv", "export.arxiv.org", "arXiv IDs", interval=3)


class Book(Entity):
    isbn: str | None = field("ISBN")
    publisher: str | None = field("Publisher")


class Books(Theme):
    id, name, version, api_version = "books_online", "Books", 1, 5
    entities = [Book]
    online_sources = [LIBRARY]

    def online_requests(self, entity_type: type[Entity], item: Record) -> list[str]:
        isbn = item.fields["isbn"]
        if isbn == "bad":
            return ["http://openlibrary.org/isbn/bad.json"]
        return [f"https://openlibrary.org/isbn/{isbn}.json"] if isbn else []

    def online_details(
        self, entity: EntityRef, responses: Mapping[str, OnlineResponse], ctx: IngestContext
    ) -> None:
        [response] = responses.values()
        if not response.ok:
            return
        found = response.json()
        if found.get("explode"):
            raise ValueError("unexpected answer")
        ctx.update(entity, publisher=found["publishers"][0])


@dataclass
class FakeWeb:
    """An :class:`~tagalot.core.online.Opener` answering from a table, recording requests."""

    answers: dict[str, list[Reply | Exception]] = dc_field(default_factory=dict)
    requests: list[tuple[str, str]] = dc_field(default_factory=list)

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Reply:
        self.requests.append((url, headers["User-Agent"]))
        queue = self.answers.get(url) or [Reply(404, b'{"error": "notfound"}')]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def urls(self) -> list[str]:
        return [u for u, _ in self.requests]


def _book_reply(publisher: str) -> Reply:
    return Reply(200, f'{{"publishers": ["{publisher}"]}}'.encode())


@dataclass
class Clock:
    now: float = 100.0
    slept: list[float] = dc_field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@dataclass
class Env:
    engine: Engine
    reader: Engine
    writer: DbWriter
    schema: ThemeSchema
    web: FakeWeb

    def books(self, *isbns: str | None) -> list[int]:
        with self.engine.begin() as conn:
            ctx = IngestSession(conn, self.schema)
            refs = [
                ctx.upsert(Book, f"b{n}", title=f"Book {n}", isbn=isbn)
                for n, isbn in enumerate(isbns)
            ]
            ctx.flush()
        return [r.id for r in refs]

    def fetcher(self) -> Fetcher:
        clock = Clock()
        return Fetcher(Books.online_sources, opener=self.web, clock=clock, sleep=clock.sleep)

    def look_up(self, ids: list[int], *, refresh: bool = False) -> LookupReport:
        return look_up(
            self.writer,
            self.reader,
            self.schema,
            Books(),
            ids,
            refresh=refresh,
            fetcher=self.fetcher(),
        )

    def publisher(self, entity_id: int) -> tuple[str | None, FieldSource | None]:
        with self.engine.connect() as conn:
            ctx = IngestSession(conn, self.schema)
            value = ctx.get(EntityRef(entity_id, "books_online.book")).fields["publisher"]
            source = conn.scalar(
                select(FieldProvenance.source).where(
                    FieldProvenance.entity_id == entity_id, FieldProvenance.field == "publisher"
                )
            )
        return value, source


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("books_online", 1)))
    schema = open_theme(engine, keep, Books).schema
    writer = DbWriter(create_keep_engine(keep.db_path))
    reader = create_keep_engine(keep.db_path, read_only=True)
    yield Env(engine, reader, writer, schema, FakeWeb())
    writer.close()
    reader.dispose()
    engine.dispose()


# --- declaring ---


def test_sources_are_checked_when_declared() -> None:
    with pytest.raises(ThemeDeclarationError, match="lowercase host name"):
        OnlineSource("Crossref", "https://api.crossref.org", "DOIs")
    with pytest.raises(ThemeDeclarationError, match="lowercase host name"):
        OnlineSource("Crossref", "api.crossref.org/works", "DOIs")
    with pytest.raises(ThemeDeclarationError, match="needs a name and what it is sent"):
        OnlineSource("Crossref", "api.crossref.org", " ")


def test_online_sources_need_api_version_5_and_distinct_hosts() -> None:
    class Old(Books):
        api_version = 4

    class Twice(Books):
        online_sources = [LIBRARY, OnlineSource("Again", "openlibrary.org", "ISBNs")]

    assert validate_theme(Books) == []
    assert "online_sources must set api_version = 5 (or later)" in " ".join(validate_theme(Old))
    assert "online_sources names a host twice" in validate_theme(Twice)


def test_only_https_on_a_declared_host() -> None:
    sources = [LIBRARY, ARXIV]
    assert source_for("https://openlibrary.org/isbn/1.json", sources) is LIBRARY
    assert source_for("https://OpenLibrary.org:443/isbn/1.json", sources) is LIBRARY
    refused = [
        "http://openlibrary.org/isbn/1.json",
        "https://openlibrary.org:8080/isbn/1.json",
        "https://me:secret@openlibrary.org/isbn/1.json",
        "https://evil.example/isbn/1.json",
        "https://openlibrary.org.evil.example/x",
        "file:///etc/passwd",
        "https://[bad/",
    ]
    for url in refused:
        with pytest.raises(OnlineError):
            source_for(url, sources)


def test_the_consent_text_names_each_service() -> None:
    assert consent_text([LIBRARY]) == (
        "Tagalot would send ISBNs to Open Library. Nothing else about your files leaves "
        "this computer."
    )
    crossref = OnlineSource("Crossref", "api.crossref.org", "DOIs")
    assert consent_text([crossref, ARXIV, LIBRARY]).startswith(
        "Tagalot would send DOIs to Crossref, arXiv IDs to arXiv and ISBNs to Open Library."
    )


# --- fetching ---


def test_requests_to_a_host_are_spaced_out() -> None:
    web = FakeWeb()
    clock = Clock()
    fetcher = Fetcher([LIBRARY, ARXIV], opener=web, clock=clock, sleep=clock.sleep)
    fetcher.fetch("https://export.arxiv.org/api/query?id_list=1")
    fetcher.fetch("https://openlibrary.org/isbn/1.json")  # another host: no wait
    assert clock.slept == []
    clock.now += 1
    fetcher.fetch("https://export.arxiv.org/api/query?id_list=2")
    assert clock.slept == [2]  # arXiv asks for three seconds
    assert all(agent.startswith("Tagalot/") for _, agent in web.requests)


def test_unknown_identifiers_are_answers_and_busy_services_are_tried_again() -> None:
    url = "https://openlibrary.org/isbn/1.json"
    web = FakeWeb({url: [Reply(429, b"", retry_after=7), Reply(503, b""), _book_reply("Tor")]})
    clock = Clock()
    fetcher = Fetcher([LIBRARY], opener=web, clock=clock, sleep=clock.sleep)
    answer = fetcher.fetch(url)
    assert (answer.status, answer.json()) == (200, {"publishers": ["Tor"]})
    assert clock.slept[:2] == [7, 10]  # Retry-After, then the second backoff
    missing = fetcher.fetch("https://openlibrary.org/isbn/2.json")
    assert (missing.status, missing.ok) == (404, False)


def test_a_service_that_keeps_failing_is_given_up_on() -> None:
    web = FakeWeb({"https://openlibrary.org/isbn/1.json": [Reply(500, b"")]})
    clock = Clock()
    fetcher = Fetcher([LIBRARY], opener=web, clock=clock, sleep=clock.sleep)
    with pytest.raises(UnavailableError, match="Open Library: is busy or failing"):
        fetcher.fetch("https://openlibrary.org/isbn/1.json")
    assert len(web.requests) == 3
    with pytest.raises(UnavailableError):
        fetcher.fetch("https://openlibrary.org/isbn/2.json")
    assert len(web.requests) == 3  # not asked again this run


def test_an_unreachable_service() -> None:
    web = FakeWeb({"https://openlibrary.org/isbn/1.json": [OSError("no route to host")]})
    fetcher = Fetcher([LIBRARY], opener=web, sleep=lambda s: None)
    with pytest.raises(UnavailableError, match="can't be reached"):
        fetcher.fetch("https://openlibrary.org/isbn/1.json")


# --- looking up ---


def test_details_are_fetched_once_kept_and_marked_fetched(env: Env) -> None:
    dune, unknown, none = env.books("1", "2", None)
    env.web.answers["https://openlibrary.org/isbn/1.json"] = [_book_reply("Chilton")]
    report = env.look_up([dune, unknown, none])
    assert env.web.urls == [
        "https://openlibrary.org/isbn/1.json",
        "https://openlibrary.org/isbn/2.json",
    ]
    assert (report.items, report.fetched, report.not_found) == (2, 2, 1)
    assert env.publisher(dune) == ("Chilton", FieldSource.FETCHED)
    assert env.publisher(unknown) == (None, None)
    with env.engine.connect() as conn:
        kept = dict(conn.execute(select(CachedResponse.url, CachedResponse.status)).all())
    assert kept == {
        "https://openlibrary.org/isbn/1.json": 200,
        "https://openlibrary.org/isbn/2.json": 404,  # unknown: not asked again either
    }

    env.web.requests.clear()
    report = env.look_up([dune, unknown])
    assert env.web.urls == []  # answered from the keep
    assert report.items == 2

    env.web.answers["https://openlibrary.org/isbn/1.json"] = [_book_reply("Ace")]
    env.look_up([dune], refresh=True)  # Look up online fetches again
    assert env.web.urls == ["https://openlibrary.org/isbn/1.json"]
    assert env.publisher(dune) == ("Ace", FieldSource.FETCHED)


def test_the_users_edits_win(env: Env) -> None:
    [dune] = env.books("1")
    with env.engine.begin() as conn:
        edit_field(conn, env.schema, dune, "publisher", "Mine")
    env.web.answers["https://openlibrary.org/isbn/1.json"] = [_book_reply("Chilton")]
    env.look_up([dune])
    assert env.publisher(dune) == ("Mine", FieldSource.USER)


def test_files_read_again_win_over_looked_up_details(env: Env) -> None:
    [dune] = env.books("1")
    env.web.answers["https://openlibrary.org/isbn/1.json"] = [_book_reply("Chilton")]
    env.look_up([dune])
    with env.engine.begin() as conn:
        IngestSession(conn, env.schema).update(EntityRef(dune, "books_online.book"), publisher="X")
    assert env.publisher(dune) == ("X", FieldSource.EXTRACTED)


def test_an_unreachable_service_changes_nothing_and_is_tried_next_time(env: Env) -> None:
    [dune] = env.books("1")
    env.web.answers["https://openlibrary.org/isbn/1.json"] = [OSError("offline")]
    report = env.look_up([dune])
    assert (report.items, report.skipped) == (0, 1)
    assert report.problems == ["Open Library: can't be reached (offline)"]
    with env.engine.connect() as conn:
        assert conn.scalar(select(CachedResponse.url)) is None
    env.web.answers["https://openlibrary.org/isbn/1.json"] = [_book_reply("Chilton")]
    env.look_up([dune])
    assert env.publisher(dune) == ("Chilton", FieldSource.FETCHED)


def test_theme_mistakes_are_reported_and_the_rest_go_ahead(env: Env) -> None:
    bad_url, exploding, fine = env.books("bad", "boom", "1")
    env.web.answers["https://openlibrary.org/isbn/boom.json"] = [Reply(200, b'{"explode": 1}')]
    env.web.answers["https://openlibrary.org/isbn/1.json"] = [_book_reply("Chilton")]
    report = env.look_up([bad_url, exploding, fine])
    assert env.web.urls == [
        "https://openlibrary.org/isbn/boom.json",
        "https://openlibrary.org/isbn/1.json",
    ]  # the http address was never fetched
    assert report.problems == [
        "Book 0: 'http://openlibrary.org/isbn/bad.json': only plain https addresses are looked up",
        "Book 1: the theme failed to read what was found: unexpected answer",
    ]
    assert report.items == 1
    assert env.publisher(fine) == ("Chilton", FieldSource.FETCHED)


# --- consent ---


def test_consent_is_saved_in_keep_toml(tmp_path: Path) -> None:
    keep = create_keep(tmp_path / "k", "K", ThemeRef("generic", 1))
    with KeepSession.open(keep.dir, Settings()) as session:
        assert session.keep.config.online_lookups is None
        with pytest.raises(NotAllowedError):
            session.look_up([1])
        session.set_online_lookups("allow")
        assert session.look_up([1]).items == 0  # the generic theme looks nothing up
        session.set_online_lookups("never")
        with pytest.raises(NotAllowedError):
            session.look_up([1])
    text = keep.toml_path.read_text(encoding="utf-8")
    assert "[online]\nlookups = 'never'\n" in text
    assert load_keep_config(keep.toml_path).online_lookups == "never"
    keep.toml_path.write_text(text.replace("'never'", "'sometimes'"), encoding="utf-8")
    with pytest.raises(KeepConfigError, match='lookups must be "allow" or "never"'):
        load_keep_config(keep.toml_path)


def test_redirects_are_followed_only_on_the_same_host() -> None:
    """Open Library sends /isbn/… to /books/…; a redirect anywhere else is refused, so
    nothing goes to a host the theme didn't declare."""
    redirects = _SameHostRedirects()
    request = urllib.request.Request("https://openlibrary.org/isbn/1.json")
    followed = redirects.redirect_request(
        request, io.BytesIO(), 302, "Found", HTTPMessage(), "https://openlibrary.org/books/1"
    )
    assert followed is not None
    assert followed.full_url == "https://openlibrary.org/books/1"
    for elsewhere in [
        "https://evil.example/x",
        "http://openlibrary.org/x",
        "https://openlibrary.org:8443/x",
    ]:
        with pytest.raises(urllib.error.HTTPError):
            redirects.redirect_request(
                request, io.BytesIO(), 302, "Found", HTTPMessage(), elsewhere
            )
