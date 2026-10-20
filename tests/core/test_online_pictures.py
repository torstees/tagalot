"""Pictures from online services (#483, DESIGN.md §9 *Online details*, §10): named by the
theme from what it found, fetched after the details from the services the keep allows, kept
in the keep, and shown only when an item has no picture of its own."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.ingest import IngestError, IngestSession
from tagalot.core.keep import (
    LEGACY_HOSTS,
    ThemeRef,
    allowed_hosts,
    create_keep,
    load_keep_config,
    save_keep_config,
)
from tagalot.core.keep_settings import with_online_lookups, with_online_services
from tagalot.core.models import OnlinePicture
from tagalot.core.online import Fetcher, LookupReport, Reply, look_up
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.thumbnails.cache import ThumbCache
from tagalot.core.thumbnails.resolve import ThumbnailResolver
from tagalot.core.writer import DbWriter
from tagalot.themes.api import (
    Entity,
    EntityRef,
    IngestContext,
    OnlineResponse,
    OnlineSource,
    Record,
    Theme,
    field,
)
from tests.core.book_files import jpeg
from tests.core.test_online import Clock, FakeWeb

LIBRARY = OnlineSource("Open Library", "openlibrary.org", "ISBNs")
COVERS = OnlineSource("Open Library Covers", "covers.openlibrary.org", "ISBNs")


class Book(Entity):
    isbn: str | None = field("ISBN")


class Books(Theme):
    id, name, version, api_version = "books_pictures", "Books", 1, 21
    entities = [Book]
    online_sources = [LIBRARY, COVERS]

    def online_requests(self, entity_type: type[Entity], item: Record) -> list[str]:
        isbn = item.fields["isbn"]
        return [f"https://openlibrary.org/isbn/{isbn}.json"] if isbn else []

    def online_details(
        self, entity: EntityRef, responses: Mapping[str, OnlineResponse], ctx: IngestContext
    ) -> None:
        [answer] = responses.values()
        if not answer.ok:
            return
        cover = answer.json().get("cover")
        if cover:
            ctx.online_picture(entity, cover)


THEME = """
from tagalot.themes.api import Entity, OnlineSource, Theme, field


class Book(Entity):
    isbn: str | None = field("ISBN")


class Books(Theme):
    id, name, version, api_version = "books_pictures", "Books", 1, 21
    entities = [Book]
    online_sources = [
        OnlineSource("Open Library", "openlibrary.org", "ISBNs"),
        OnlineSource("Open Library Covers", "covers.openlibrary.org", "ISBNs"),
    ]
"""
"""The same services, as a theme file for a keep."""


def _edition(cover: str | None) -> Reply:
    return Reply(200, (f'{{"cover": "{cover}"}}' if cover else "{}").encode())


def _cover(isbn: str) -> str:
    return f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"


@dataclass
class Env:
    engine: Engine
    reader: Engine
    writer: DbWriter
    schema: ThemeSchema
    web: FakeWeb
    tmp_path: Path

    def books(self, *isbns: str) -> list[int]:
        with self.engine.begin() as conn:
            ctx = IngestSession(conn, self.schema)
            refs = [ctx.upsert(Book, f"b{isbn}", title=f"Book {isbn}", isbn=isbn) for isbn in isbns]
            ctx.flush()
        return [r.id for r in refs]

    def look_up(
        self,
        ids: list[int],
        *,
        refresh: bool = False,
        sources: tuple[OnlineSource, ...] = (LIBRARY, COVERS),
    ) -> LookupReport:
        clock = Clock()
        fetcher = Fetcher(sources, opener=self.web, clock=clock, sleep=clock.sleep)
        return look_up(
            self.writer, self.reader, self.schema, Books(), ids, refresh=refresh, fetcher=fetcher
        )

    def kept(self) -> dict[int, tuple[str, int | None, bool]]:
        """Each item's picture: its address, the status it got, and whether it has one."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    OnlinePicture.entity_id,
                    OnlinePicture.url,
                    OnlinePicture.status,
                    OnlinePicture.data,
                )
            )
            return {r.entity_id: (r.url, r.status, r.data is not None) for r in rows}

    def resolver(self) -> ThumbnailResolver:
        cache = ThumbCache(self.tmp_path / "thumbs.db")
        return ThumbnailResolver(self.reader, self.writer, Books, cache, str, 64, self.schema)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(
        create_keep(tmp_path / "k", "K", ThemeRef("books_pictures", 1))
    )
    schema = open_theme(engine, keep, Books).schema
    writer = DbWriter(create_keep_engine(keep.db_path))
    reader = create_keep_engine(keep.db_path, read_only=True)
    yield Env(engine, reader, writer, schema, FakeWeb(), tmp_path)
    writer.close()
    reader.dispose()
    engine.dispose()


def test_a_picture_is_fetched_after_the_details_and_kept(env: Env) -> None:
    dune, plain, missing = env.books("1", "2", "3")
    env.web.answers["https://openlibrary.org/isbn/1.json"] = [_edition(_cover("1"))]
    env.web.answers["https://openlibrary.org/isbn/2.json"] = [_edition(None)]
    env.web.answers["https://openlibrary.org/isbn/3.json"] = [_edition(_cover("3"))]
    env.web.answers[_cover("1")] = [Reply(200, jpeg())]  # 3's isn't there: a 404
    report = env.look_up([dune, plain, missing])
    assert env.web.urls == [
        "https://openlibrary.org/isbn/1.json",
        "https://openlibrary.org/isbn/2.json",
        "https://openlibrary.org/isbn/3.json",
        _cover("1"),
        _cover("3"),
    ]
    assert report.pictures == [dune]
    assert report.not_found == 1
    assert env.kept() == {dune: (_cover("1"), 200, True), missing: (_cover("3"), 404, False)}

    env.web.requests.clear()
    env.look_up([dune, plain, missing])  # kept: nothing is fetched again
    assert env.web.urls == []

    env.look_up([dune], refresh=True)  # Look up online fetches the picture again too
    assert env.web.urls == ["https://openlibrary.org/isbn/1.json", _cover("1")]


def test_another_address_replaces_the_picture(env: Env) -> None:
    [dune] = env.books("1")
    env.web.answers["https://openlibrary.org/isbn/1.json"] = [_edition(_cover("1"))]
    env.web.answers[_cover("1")] = [Reply(200, jpeg())]
    env.look_up([dune])
    env.web.answers["https://openlibrary.org/isbn/1.json"] = [_edition(_cover("1b"))]
    env.web.answers[_cover("1b")] = [Reply(200, jpeg(color=(0, 0, 255)))]
    env.look_up([dune], refresh=True)
    assert env.kept() == {dune: (_cover("1b"), 200, True)}


def test_a_service_the_keep_hasnt_allowed_is_left_alone(env: Env) -> None:
    [dune] = env.books("1")
    env.web.answers["https://openlibrary.org/isbn/1.json"] = [_edition(_cover("1"))]
    report = env.look_up([dune], sources=(LIBRARY,))  # the covers service isn't allowed
    assert env.web.urls == ["https://openlibrary.org/isbn/1.json"]
    assert report.items == 1  # the details go ahead
    assert env.kept() == {dune: (_cover("1"), None, False)}  # named, waiting
    assert report.problems == []

    env.web.requests.clear()
    env.look_up([dune])  # allowed now: fetched, from the kept details
    assert env.web.urls == [_cover("1")]


def test_a_picture_must_be_on_a_service_of_the_theme(env: Env) -> None:
    [dune] = env.books("1")
    env.web.answers["https://openlibrary.org/isbn/1.json"] = [
        _edition("https://example.com/cover.jpg")
    ]
    report = env.look_up([dune])
    assert env.kept() == {}
    assert len(report.problems) == 1
    assert "example.com isn't one of the theme's services" in report.problems[0]


def test_only_online_details_names_pictures(env: Env) -> None:
    [dune] = env.books("1")
    with env.engine.begin() as conn, pytest.raises(IngestError):
        IngestSession(conn, env.schema).online_picture(
            EntityRef(dune, "books_pictures.book"), _cover("1")
        )


def test_the_picture_shows_when_the_item_has_none_of_its_own(env: Env) -> None:
    dune, plain = env.books("1", "2")
    resolver = env.resolver()
    try:
        assert resolver.resolve(dune).icon == "entity"  # nothing yet
        env.web.answers["https://openlibrary.org/isbn/1.json"] = [_edition(_cover("1"))]
        env.web.answers[_cover("1")] = [Reply(200, jpeg())]
        env.look_up([dune, plain])
        shown = resolver.resolve(dune)
        assert shown.thumbnail is not None
        assert shown.resource_id is None  # not a file: never remembered as its source
        assert resolver.resolve(plain).thumbnail is None
    finally:
        resolver.cache.close()


def test_a_picture_that_cant_be_drawn_shows_the_icon(env: Env) -> None:
    [dune] = env.books("1")
    env.web.answers["https://openlibrary.org/isbn/1.json"] = [_edition(_cover("1"))]
    env.web.answers[_cover("1")] = [Reply(200, b"<html>not a picture</html>")]
    env.look_up([dune])
    resolver = env.resolver()
    try:
        shown = resolver.resolve(dune)
    finally:
        resolver.cache.close()
    assert (shown.thumbnail, shown.icon) == (None, "entity")


# --- consent per service ---


def test_services_allowed_and_refused_are_kept_in_keep_toml(tmp_path: Path) -> None:
    keep = create_keep(tmp_path / "k", "K", ThemeRef("generic", 1))
    config = with_online_lookups(keep.config, "allow", ["openlibrary.org"])
    assert allowed_hosts(config) == {"openlibrary.org"}
    config = with_online_services(
        config, allow=["covers.openlibrary.org"], refuse=["api.themoviedb.org"]
    )
    assert allowed_hosts(config) == {"openlibrary.org", "covers.openlibrary.org"}
    assert config.online_refused == ["api.themoviedb.org"]
    save_keep_config(config, keep.toml_path)
    text = keep.toml_path.read_text(encoding="utf-8")
    assert (
        "[online]\nlookups = 'allow'\nallowed = ['covers.openlibrary.org', 'openlibrary.org']\n"
        "refused = ['api.themoviedb.org']\n"
    ) in text
    assert load_keep_config(keep.toml_path) == config
    assert allowed_hosts(with_online_lookups(config, "never")) == frozenset()


def test_a_keep_that_allowed_lookups_before_keeps_its_services(tmp_path: Path) -> None:
    keep = create_keep(tmp_path / "k", "K", ThemeRef("generic", 1))
    keep.toml_path.write_text(
        keep.toml_path.read_text(encoding="utf-8") + "\n[online]\nlookups = 'allow'\n",
        encoding="utf-8",
    )
    config = load_keep_config(keep.toml_path)
    assert config.online_allowed is None
    assert allowed_hosts(config) == LEGACY_HOSTS


def test_a_session_asks_about_services_added_since(tmp_path: Path) -> None:
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "books_pictures.py").write_text(THEME, encoding="utf-8")
    keep = create_keep(tmp_path / "k", "K", ThemeRef("books_pictures", 1))
    keep.toml_path.write_text(
        keep.toml_path.read_text(encoding="utf-8") + "\n[online]\nlookups = 'allow'\n",
        encoding="utf-8",
    )
    with KeepSession.open(keep.dir, Settings(theme_dirs=[themes])) as session:
        assert [s.name for s in session.allowed_online_sources()] == ["Open Library"]
        assert [s.name for s in session.new_online_sources()] == ["Open Library Covers"]
        session.answer_online_sources(session.new_online_sources(), allow=False)
        assert session.new_online_sources() == []  # answered: not asked again
        assert [s.name for s in session.allowed_online_sources()] == ["Open Library"]
        session.set_online_lookups("allow")  # turning lookups on allows every service
        assert [s.host for s in session.allowed_online_sources()] == [
            "openlibrary.org",
            "covers.openlibrary.org",
        ]
