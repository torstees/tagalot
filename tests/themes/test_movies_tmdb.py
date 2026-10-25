"""The movies theme's online details (#487): TMDb by TMDb ID, else IMDb ID, else (when the
keep allows it) by title; details the files lack, the poster, and the cast's photos, on the
movies demo. A fake TMDb answers; nothing goes online."""

import importlib.util
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from tagalot.builtin_themes.movies import (
    TMDB,
    Movie,
    MoviesTheme,
    same_movie,
    tmdb_id,
    tmdb_values,
)
from tagalot.core.ingest import IngestSession
from tagalot.core.models import Entity, FieldProvenance, FieldSource, OnlinePicture
from tagalot.core.online import Reply
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.themes.api import EntityRef, OnlineRequest, Record
from tests.core.book_files import jpeg

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
API = "https://api.themoviedb.org/3"
IMAGES = "https://image.tmdb.org/t/p"
HEAT_FIND = f"{API}/find/tt0113277?external_source=imdb_id"
HEAT = f"{API}/movie/949?append_to_response=credits"
MATRIX_SEARCH = f"{API}/search/movie?query=The+Matrix&year=1999"
MATRIX = f"{API}/movie/603?append_to_response=credits"


def _person(name: str, order: int, photo: str | None = None) -> dict[str, Any]:
    return {"name": name, "order": order, "profile_path": photo}


def _movie(title: str, released: str, *cast: dict[str, Any], **more: Any) -> Reply:
    record = {
        "title": title,
        "release_date": released,
        "overview": "  What   it's about. ",
        "genres": [{"id": 80, "name": "Crime"}, {"id": 18, "name": "Drama"}],
        "runtime": 170,
        "vote_average": 7.9,
        "vote_count": 7000,
        "credits": {"cast": list(cast), "crew": [{"name": "Michael Mann", "job": "Director"}]},
        **more,
    }
    return Reply(200, json.dumps(record).encode())


class FakeTmdb:
    """TMDb and its pictures, answering from a table (404 for anything else)."""

    def __init__(self) -> None:
        self.answers: dict[str, Reply] = {
            HEAT_FIND: Reply(200, b'{"movie_results": [{"id": 949, "title": "Heat"}]}'),
            HEAT: _movie(
                "Heat",
                "1995-12-15",
                _person("Al Pacino", 0, "/pacino.jpg"),
                _person("Robert De Niro", 1),
                poster_path="/heat.jpg",
            ),
            MATRIX_SEARCH: Reply(
                200,
                json.dumps(
                    {
                        "results": [  # TMDb's first isn't the film named: the second is
                            {
                                "id": 604,
                                "title": "The Matrix Reloaded",
                                "release_date": "2003-05-15",
                            },
                            {"id": 603, "title": "The Matrix", "release_date": "1999-03-30"},
                        ]
                    }
                ).encode(),
            ),
            MATRIX: _movie(
                "The Matrix",
                "1999-03-30",
                _person("Carrie-Anne Moss", 1),
                _person("Keanu Reeves", 0, "/keanu.jpg"),
                poster_path="/matrix.jpg",
            ),
        }
        for path in ("/pacino.jpg", "/keanu.jpg"):
            self.answers[f"{IMAGES}/w185{path}"] = Reply(200, jpeg())
        self.answers[f"{IMAGES}/w500/matrix.jpg"] = Reply(200, jpeg())
        self.urls: list[str] = []
        self.headers: dict[str, Mapping[str, str]] = {}

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Reply:
        self.urls.append(url)
        self.headers[url] = dict(headers)
        return self.answers.get(url, Reply(404, b'{"status_code": 34}'))


@pytest.fixture
def tmdb() -> FakeTmdb:
    return FakeTmdb()


@pytest.fixture
def session(
    tmp_path: Path, tmdb: FakeTmdb, monkeypatch: pytest.MonkeyPatch
) -> Iterator[KeepSession]:
    monkeypatch.setenv("TAGALOT_TMDB_TOKEN", "test-token")
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_movies_demo(tmp_path), Settings()) as session:
        session.online_opener = tmdb
        session.set_online_lookups("allow")
        yield session


def _entity(session: KeepSession, title: str, type_id: str = "movies.movie") -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title, Entity.type == type_id))
    assert found is not None, title
    return found


def _fields(session: KeepSession, movie: int) -> dict[str, Any]:
    table = session.schema.by_type_id("movies.movie").table
    with session.reader.connect() as conn:
        return dict(conn.execute(select(table).where(table.c.id == movie)).one()._mapping)


def _cast(session: KeepSession, movie: int) -> list[str]:
    with session.reader.connect() as conn:
        ingest = IngestSession(conn, session.schema)
        return sorted(
            ingest.get(p).title for p in ingest.related("cast", EntityRef(movie, "movies.movie"))
        )


def _pictures(session: KeepSession) -> dict[int, str]:
    with session.reader.connect() as conn:
        rows = conn.execute(
            select(OnlinePicture.entity_id, OnlinePicture.url).where(
                OnlinePicture.data.is_not(None)
            )
        )
        return {e: u for e, u in rows}


def _source(session: KeepSession, entity: int, name: str) -> FieldSource | None:
    with session.reader.connect() as conn:
        return conn.scalar(
            select(FieldProvenance.source).where(
                FieldProvenance.entity_id == entity, FieldProvenance.field == name
            )
        )


def test_what_is_looked_up() -> None:
    theme = MoviesTheme()

    def movie(title: str, **fields: Any) -> Record:
        return Record(EntityRef(1, "movies.movie"), title, fields, {})

    assert list(
        theme.online_requests(Movie, movie("Heat", tmdb_id="949", imdb_id="tt0113277"))
    ) == [HEAT]
    assert list(theme.online_requests(Movie, movie("Heat", imdb_id="tt0113277"))) == [HEAT_FIND]
    assert list(theme.online_requests(Movie, movie("The Matrix", year=1999))) == [
        OnlineRequest(MATRIX_SEARCH, by="title-year")
    ]
    assert list(theme.online_requests(Movie, movie("3rd Birthday & cake"))) == [
        OnlineRequest(f"{API}/search/movie?query=3rd+Birthday+%26+cake", by="title")
    ]
    assert list(theme.online_requests(Movie, movie("..."))) == []
    assert list(theme.online_requests(Movie, movie("Heat", imdb_id="not an id"))) != [HEAT_FIND]
    assert TMDB.token_env == "TAGALOT_TMDB_TOKEN"
    assert TMDB.titles
    assert (tmdb_id("0949"), tmdb_id(949), tmdb_id("x"), tmdb_id(None)) == (
        "949",
        "949",
        None,
        None,
    )


def test_reading_a_tmdb_movie() -> None:
    record = json.loads(_movie("Heat", "1995-12-15", vote_count=0).body)
    assert tmdb_values(record) == {
        "title": "Heat",
        "year": 1995,
        "plot": "What it's about.",
        "genre": "Crime, Drama",
        "director": "Michael Mann",
        "runtime": 170 * 60.0,
        "rating": None,  # no votes: no rating
    }
    assert tmdb_values({})["title"] is None


def test_a_search_result_is_the_movie_only_by_its_title_and_year() -> None:
    matrix = Record(EntityRef(1, "movies.movie"), "The Matrix", {"year": 1999}, {})
    assert same_movie(matrix, {"title": "the matrix", "release_date": "1999-03-30"})
    assert not same_movie(matrix, {"title": "The Matrix", "release_date": "2021-12-22"})
    assert not same_movie(matrix, {"title": "The Matrix Reloaded", "release_date": "1999-01-01"})
    fellowship = Record(EntityRef(3, "movies.movie"), "The Fellowship of the Ring", {}, {})
    lotr = "The Lord of the Rings: The Fellowship of the Ring"
    assert same_movie(fellowship, {"title": lotr, "release_date": "2001-12-18"})
    assert not same_movie(fellowship, {"title": "The Fellowship", "release_date": "2001-12-18"})
    birthday = Record(EntityRef(2, "movies.movie"), "Birthday", {"year": None}, {})
    assert same_movie(birthday, {"original_title": "Birthday", "release_date": ""})


def test_by_imdb_id_fills_in_what_the_nfo_doesnt_say(session: KeepSession, tmdb: FakeTmdb) -> None:
    heat = _entity(session, "Heat")
    report = session.look_up([heat])
    # Found by its IMDb ID, then looked up by the TMDb ID that gave, in the same run; Al
    # Pacino's photo, as Heat has its own poster.
    assert tmdb.urls == [HEAT_FIND, HEAT, f"{IMAGES}/w185/pacino.jpg"]
    assert tmdb.headers[HEAT]["Authorization"] == "Bearer test-token"
    assert "Authorization" not in tmdb.headers[f"{IMAGES}/w185/pacino.jpg"]
    fields = _fields(session, heat)
    assert fields["tmdb_id"] == "949"
    assert (fields["plot"], fields["rating"]) == ("What it's about.", 7.9)  # the .nfo has none
    assert (fields["genre"], fields["director"]) == ("Crime", "Michael Mann")  # the .nfo's
    assert fields["year"] == 1995
    assert _source(session, heat, "plot") is FieldSource.FETCHED
    assert _source(session, heat, "genre") is FieldSource.EXTRACTED
    assert _cast(session, heat) == ["Al Pacino", "Robert De Niro"]  # the .nfo's cast
    pacino = _entity(session, "Al Pacino", "movies.actor")
    assert _pictures(session) == {pacino: f"{IMAGES}/w185/pacino.jpg"}
    assert report.pictures == [pacino]


def test_titles_are_sent_only_when_the_keep_allows(session: KeepSession, tmdb: FakeTmdb) -> None:
    matrix = _entity(session, "The Matrix")
    session.look_up([matrix])
    assert tmdb.urls == []  # IDs only, by default; The Matrix has none

    shown = session.thumbnails.resolve(matrix)
    assert shown.resource_id is not None  # one of its screenshots, remembered

    session.set_online_find_by("title-year")
    session.look_up([matrix])
    assert tmdb.urls[:2] == [MATRIX_SEARCH, MATRIX]
    assert sorted(tmdb.urls[2:]) == [f"{IMAGES}/w185/keanu.jpg", f"{IMAGES}/w500/matrix.jpg"]
    fields = _fields(session, matrix)
    assert (fields["tmdb_id"], fields["director"]) == ("603", "Michael Mann")
    # No cast of its own: TMDb's, and Keanu Reeves's photo; its poster, as it has none.
    assert _cast(session, matrix) == ["Carrie-Anne Moss", "Keanu Reeves"]
    keanu = _entity(session, "Keanu Reeves", "movies.actor")
    assert _pictures(session) == {
        matrix: f"{IMAGES}/w500/matrix.jpg",
        keanu: f"{IMAGES}/w185/keanu.jpg",
    }

    # TMDb's poster beats its screenshots (OnlineImage), though one was remembered.
    shown = session.thumbnails.resolve(matrix)
    assert (shown.thumbnail is not None, shown.resource_id) == (True, None)

    # Applied again from what was kept: nothing fetched, nothing changes.
    tmdb.urls.clear()
    session.look_up([matrix])
    assert tmdb.urls == []
    assert _cast(session, matrix) == ["Carrie-Anne Moss", "Keanu Reeves"]


def test_the_title_replaces_the_file_names_only_without_an_nfo(
    session: KeepSession, tmdb: FakeTmdb
) -> None:
    fellowship = _entity(session, "The Fellowship of the Ring")
    session.writer.run(
        lambda conn: IngestSession(conn, session.schema).update(
            EntityRef(fellowship, "movies.movie"), tmdb_id="120"
        )
    )
    tmdb.answers[f"{API}/movie/120?append_to_response=credits"] = _movie(
        "The Lord of the Rings: The Fellowship of the Ring", "2001-12-18"
    )
    session.look_up([fellowship])
    with session.reader.connect() as conn:
        title = conn.scalar(select(Entity.title).where(Entity.id == fellowship))
    assert title == "The Lord of the Rings: The Fellowship of the Ring"
    assert _fields(session, fellowship)["runtime"] == 10680  # its video's, not TMDb's

    alien = _entity(session, "Alien")  # its .nfo names it, and gives TMDb ID 348
    tmdb.answers[f"{API}/movie/348?append_to_response=credits"] = _movie(
        "Alien²",
        "1979-05-25",
        _person("Tom Skerritt", 0, "/skerritt.jpg"),
        _person("Sigourney Weaver", 1, "/weaver.jpg"),
    )
    tmdb.urls.clear()
    session.look_up([alien])
    with session.reader.connect() as conn:
        assert conn.scalar(select(Entity.title).where(Entity.id == alien)) == "Alien"
    assert _cast(session, alien) == ["Sigourney Weaver"]  # the .nfo's, alone
    # She has photos of her own, in the notes folder: hers isn't fetched.
    assert f"{IMAGES}/w185/weaver.jpg" not in tmdb.urls


def test_without_a_token_tmdb_isnt_used(
    session: KeepSession, tmdb: FakeTmdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAGALOT_TMDB_TOKEN")
    session.look_up([_entity(session, "Heat")])
    assert tmdb.urls == []
