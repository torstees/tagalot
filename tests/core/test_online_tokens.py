"""Services that need a token, and what may identify an item (#485, DESIGN.md §9 *Online
details*): a token from an environment variable, sent as a bearer token and kept nowhere; a
service without one isn't used or asked about; and addresses that find an item by title
sent only when the keep allows it, the ID a search finds being looked up in the same run."""

import logging
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import KeepConfigError, KeepError, ThemeRef, create_keep, load_keep_config
from tagalot.core.keep_settings import with_online_find_by
from tagalot.core.models import CachedResponse
from tagalot.core.online import (
    Fetcher,
    LookupReport,
    Reply,
    consent_text,
    look_up,
    needed_env,
    tokens_text,
    usable,
)
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings, load_settings, save_settings
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import (
    Entity,
    EntityRef,
    IngestContext,
    OnlineRequest,
    OnlineResponse,
    OnlineSource,
    Record,
    Theme,
    ThemeDeclarationError,
    field,
)
from tagalot.themes.loader import validate_theme
from tests.core.test_online import Clock

TOKEN_ENV = "TAGALOT_TEST_FILMS_TOKEN"
FILMS = OnlineSource("Films DB", "api.films.test", "film IDs", token_env=TOKEN_ENV, titles=True)
API = "https://api.films.test"


class Film(Entity):
    film_id: str | None = field("Film ID")
    year: int | None = field("Year")
    plot: str | None = field("Plot")


class Films(Theme):
    id, name, version, api_version = "films_online", "Films", 1, 23
    entities = [Film]
    online_sources = [FILMS]

    def online_requests(self, entity_type: type[Entity], item: Record) -> list[str | OnlineRequest]:
        film_id, year = item.fields["film_id"], item.fields["year"]
        if film_id:
            return [f"{API}/film/{film_id}"]
        searches: list[str | OnlineRequest] = [
            OnlineRequest(f"{API}/search?q={item.title}", "title")
        ]
        if year:
            searches.insert(0, OnlineRequest(f"{API}/search?q={item.title}&y={year}", "title-year"))
        return searches[:1]

    def online_details(
        self, entity: EntityRef, responses: Mapping[str, OnlineResponse], ctx: IngestContext
    ) -> None:
        for response in responses.values():
            if not response.ok:
                continue
            found = response.json()
            if "/search" in response.url:
                ctx.update(entity, film_id=found["id"])  # then looked up by ID
            else:
                ctx.update(entity, plot=found["plot"])


THEME = """
from tagalot.themes.api import Entity, OnlineSource, Theme, field


class Film(Entity):
    film_id: str | None = field("Film ID")


class Films(Theme):
    id, name, version, api_version = "films_tokens", "Films", 1, 23
    entities = [Film]
    online_sources = [
        OnlineSource(
            "Films DB", "api.films.test", "film IDs", token_env="TAGALOT_TEST_FILMS_TOKEN"
        ),
        OnlineSource("Film Posters", "posters.films.test", "film IDs"),
    ]
"""
"""A theme file for a keep: a service needing a token, and one that doesn't."""


@dataclass
class Web:
    """An opener answering from a table, recording each request's headers."""

    answers: dict[str, Reply] = dc_field(default_factory=dict)
    requests: list[tuple[str, dict[str, str]]] = dc_field(default_factory=list)

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Reply:
        self.requests.append((url, dict(headers)))
        return self.answers.get(url, Reply(404, b"{}"))

    @property
    def urls(self) -> list[str]:
        return [u for u, _ in self.requests]


@dataclass
class Env:
    engine: Engine
    reader: Engine
    writer: DbWriter
    schema: ThemeSchema
    web: Web

    def films(self, *films: tuple[str, int | None, str | None]) -> list[int]:
        with self.engine.begin() as conn:
            ctx = IngestSession(conn, self.schema)
            refs = [
                ctx.upsert(Film, f"f{n}", title=title, year=year, film_id=film_id)
                for n, (title, year, film_id) in enumerate(films)
            ]
            ctx.flush()
        return [r.id for r in refs]

    def look_up(self, ids: list[int], find_by: str = "ids") -> LookupReport:
        clock = Clock()
        fetcher = Fetcher(
            usable(Films.online_sources), opener=self.web, clock=clock, sleep=clock.sleep
        )
        return look_up(
            self.writer, self.reader, self.schema, Films(), ids, find_by=find_by, fetcher=fetcher
        )

    def fields(self, entity_id: int) -> tuple[str | None, str | None]:
        with self.engine.connect() as conn:
            found = IngestSession(conn, self.schema).get(EntityRef(entity_id, "films_online.film"))
        return found.fields["film_id"], found.fields["plot"]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("films_online", 1)))
    schema = open_theme(engine, keep, Films).schema
    writer = DbWriter(create_keep_engine(keep.db_path))
    reader = create_keep_engine(keep.db_path, read_only=True)
    yield Env(engine, reader, writer, schema, Web())
    writer.close()
    reader.dispose()
    engine.dispose()


def test_declaring_tokens_and_titles() -> None:
    with pytest.raises(ThemeDeclarationError, match="environment variable's name"):
        OnlineSource("Films DB", "api.films.test", "film IDs", token_env="NOT A NAME")
    with pytest.raises(ValueError, match="'id', 'title-year', or 'title'"):
        OnlineRequest(f"{API}/search?q=Heat", "name")  # type: ignore[arg-type]

    class Old(Films):
        api_version = 22

    assert validate_theme(Films) == []
    assert "token_env or titles must set api_version = 23" in " ".join(validate_theme(Old))


def test_a_token_is_sent_only_while_set_and_kept_nowhere(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    [heat] = env.films(("Heat", 1995, "949"))
    env.web.answers[f"{API}/film/949"] = Reply(200, b'{"plot": "A heist."}')
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    assert usable(Films.online_sources) == []
    assert "set TAGALOT_TEST_FILMS_TOKEN to yours under Environment" in tokens_text([FILMS])
    report = env.look_up([heat])
    assert env.web.urls == []  # not set: the service isn't used, and that isn't a problem
    assert report.problems == []

    monkeypatch.setenv(TOKEN_ENV, " s3cret-token ")
    assert tokens_text([FILMS]) == ""
    with caplog.at_level(logging.DEBUG):
        env.look_up([heat])
    [(url, headers)] = env.web.requests
    assert url == f"{API}/film/949"
    assert headers["Authorization"] == "Bearer s3cret-token"
    assert env.fields(heat) == ("949", "A heist.")
    assert "s3cret" not in caplog.text
    with env.engine.connect() as conn:
        assert "s3cret" not in " ".join(
            f"{u} {b}" for u, b in conn.execute(select(CachedResponse.url, CachedResponse.body))
        )


def test_titles_are_sent_only_when_the_keep_allows_and_the_id_found_is_looked_up(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "token")
    heat, home = env.films(("Heat", 1995, None), ("Birthday 2003", None, None))
    env.web.answers[f"{API}/search?q=Heat&y=1995"] = Reply(200, b'{"id": "949"}')
    env.web.answers[f"{API}/search?q=Birthday 2003"] = Reply(200, b'{"id": "1"}')
    env.web.answers[f"{API}/film/949"] = Reply(200, b'{"plot": "A heist."}')

    report = env.look_up([heat, home])  # IDs only, the default
    assert env.web.urls == []
    assert report.problems == []

    env.look_up([heat, home], "title-year")
    # Heat is found by its title and year, then by the ID found, in the same run; the home
    # video has no year, so nothing about it is sent.
    assert env.web.urls == [f"{API}/search?q=Heat&y=1995", f"{API}/film/949"]
    assert env.fields(heat) == ("949", "A heist.")
    assert env.fields(home) == (None, None)

    env.web.requests.clear()
    env.look_up([heat, home], "title")
    assert env.web.urls == [f"{API}/search?q=Birthday 2003", f"{API}/film/1"]
    assert env.fields(home)[0] == "1"


def test_a_title_search_needs_a_service_declared_with_titles(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "token")

    class Undeclared(Films):
        online_sources = [
            OnlineSource("Films DB", "api.films.test", "film IDs", token_env=TOKEN_ENV)
        ]

    [heat] = env.films(("Heat", 1995, None))
    report = look_up(
        env.writer,
        env.reader,
        env.schema,
        Undeclared(),
        [heat],
        find_by="title",
        fetcher=Fetcher([]),
    )
    assert report.problems == [
        f"Heat: '{API}/search?q=Heat&y=1995': Films DB isn't declared with titles=True"
    ]


def test_the_consent_says_titles_may_be_sent() -> None:
    plain = OnlineSource("Crossref", "api.crossref.org", "DOIs")
    assert consent_text([FILMS, plain]) == (
        "Tagalot would send film IDs to Films DB and DOIs to Crossref. Nothing else about your "
        "files leaves this computer."
    )
    assert consent_text([FILMS, plain], "title-year").startswith(
        "Tagalot would send film IDs, or titles with their years, to Films DB and DOIs to Crossref."
    )
    assert consent_text([FILMS], "title").startswith(
        "Tagalot would send film IDs, or titles, to Films DB."
    )


def test_find_by_is_kept_in_keep_toml_and_tokens_decide_the_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "films_tokens.py").write_text(THEME, encoding="utf-8")
    keep = create_keep(tmp_path / "k", "K", ThemeRef("films_tokens", 1))
    assert load_keep_config(keep.toml_path).online_find_by == "ids"
    with KeepSession.open(keep.dir, Settings(theme_dirs=[themes])) as session:
        # Without its token, a service is left out of the question and of what's allowed.
        assert [s.name for s in session.online_sources()] == ["Film Posters"]
        session.set_online_lookups("allow")
        assert [s.name for s in session.allowed_online_sources()] == ["Film Posters"]
        assert session.new_online_sources() == []
        monkeypatch.setenv(TOKEN_ENV, "token")
        # Once it is set, it's a service the keep hasn't been asked about.
        assert [s.name for s in session.new_online_sources()] == ["Films DB"]
        assert [s.name for s in session.allowed_online_sources()] == ["Film Posters"]
        session.set_online_find_by("title-year")
        assert session.keep.config.online_find_by == "title-year"
    text = keep.toml_path.read_text(encoding="utf-8")
    assert "find_by = 'title-year'\n" in text
    assert "TAGALOT_TEST_FILMS_TOKEN" not in text
    assert "'token'" not in text
    assert load_keep_config(keep.toml_path).online_find_by == "title-year"
    keep.toml_path.write_text(text.replace("'title-year'", "'names'"), encoding="utf-8")
    with pytest.raises(KeepConfigError, match='find_by must be "ids", "title-year", or "title"'):
        load_keep_config(keep.toml_path)
    other = create_keep(tmp_path / "other", "Other", ThemeRef("generic", 1))
    with pytest.raises(KeepError, match="not 'names'"):
        with_online_find_by(other.config, "names")


def test_the_keeps_environment_wins_over_the_real_one(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#544: a keep's variables, from the user's settings, ahead of the environment."""
    monkeypatch.setenv(TOKEN_ENV, "from-the-system")
    [heat] = env.films(("Heat", 1995, "949"))
    assert usable([FILMS], {}) == [FILMS]
    clock = Clock()
    fetcher = Fetcher(
        [FILMS],
        opener=env.web,
        clock=clock,
        sleep=clock.sleep,
        env={TOKEN_ENV: "from-the-keep"},
    )
    look_up(env.writer, env.reader, env.schema, Films(), [heat], fetcher=fetcher)
    [(_, headers)] = env.web.requests
    assert headers["Authorization"] == "Bearer from-the-keep"

    monkeypatch.delenv(TOKEN_ENV)
    assert usable([FILMS]) == []
    assert usable([FILMS], {TOKEN_ENV: "from-the-keep"}) == [FILMS]
    assert tokens_text([FILMS], {TOKEN_ENV: "from-the-keep"}) == ""
    assert needed_env([FILMS, OnlineSource("Other", "other.test", "IDs")]) == [
        (TOKEN_ENV, "Films DB")
    ]


def test_a_keeps_environment_is_kept_in_the_users_settings(tmp_path: Path) -> None:
    keep_id = uuid.uuid4()
    settings = Settings()
    settings.set_env(keep_id, "TAGALOT_TMDB_TOKEN", "  secret  ")
    settings.set_env(keep_id, "OTHER", "x")
    with pytest.raises(ValueError, match="can't name an environment variable"):
        settings.set_env(keep_id, "1 BAD", "x")
    path = tmp_path / "settings.toml"
    save_settings(settings, path)
    text = path.read_text(encoding="utf-8")
    assert f"[environment.{keep_id}]\nTAGALOT_TMDB_TOKEN = 'secret'\nOTHER = 'x'\n" in text
    loaded = load_settings(path)
    assert loaded.environment(keep_id) == {"TAGALOT_TMDB_TOKEN": "secret", "OTHER": "x"}
    loaded.set_env(keep_id, "OTHER", "")
    loaded.set_env(keep_id, "TAGALOT_TMDB_TOKEN", None)
    assert loaded.environment(keep_id) == {}
    assert keep_id not in loaded.keep_env
    path.write_text(
        f"[environment.'{keep_id}']\nGOOD = 'yes'\nBAD = 3\n'not a name' = 'no'\n",
        encoding="utf-8",
    )
    assert load_settings(path).environment(keep_id) == {"GOOD": "yes"}  # the rest skipped


def test_a_session_uses_its_keeps_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "films_tokens.py").write_text(THEME, encoding="utf-8")
    keep = create_keep(tmp_path / "k", "K", ThemeRef("films_tokens", 1))
    settings = Settings(theme_dirs=[themes])
    settings.set_env(keep.config.id, TOKEN_ENV, "s3cret-value")
    with KeepSession.open(keep.dir, settings) as session:
        assert session.environment() == {TOKEN_ENV: "s3cret-value"}
        assert [s.name for s in session.online_sources()] == ["Films DB", "Film Posters"]
        assert "s3cret" not in keep.toml_path.read_text(encoding="utf-8")
