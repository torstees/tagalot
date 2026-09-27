"""Tests for building theme tables from declarations (DESIGN.md §5, §9)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, delete, exc, insert, inspect, select

from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Entity
from tagalot.core.theme_schema import SchemaBuildError, build_theme_schema
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import Theme, field, related
from tests.themes.test_api import Actor, Collection, Movie, MoviesTheme


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_design_example_builds_the_expected_tables() -> None:
    schema = build_theme_schema(MoviesTheme)
    assert set(schema.metadata.tables) == {
        "movies_actor",
        "movies_collection",
        "movies_movie",
        "movies_cast",
    }
    actor = schema.entities[Actor].table
    assert [c.name for c in actor.columns] == ["id", "born", "country"]
    assert actor.c.born.nullable
    assert schema.entities[Movie].type_id == "movies.movie"
    assert schema.by_type_id("movies.collection").entity is Collection
    with pytest.raises(KeyError):
        schema.by_type_id("movies.nope")
    assert set(schema.relationships) == {"cast"}


def test_tables_are_created_next_to_the_core_schema(engine: Engine) -> None:
    schema = build_theme_schema(MoviesTheme)
    schema.metadata.create_all(engine)
    insp = inspect(engine)
    assert {"movies_movie", "movies_cast", "entity"} <= set(insp.get_table_names())
    [fk] = insp.get_foreign_keys("movies_movie")
    assert (fk["referred_table"], fk["referred_columns"], fk["options"].get("ondelete")) == (
        "entity",
        ["id"],
        "CASCADE",
    )
    indexed = {ix["column_names"][0] for ix in insp.get_indexes("movies_actor")}
    assert indexed == {"born", "country"}  # range and choice fields


def test_rows_cascade_with_their_entity(engine: Engine) -> None:
    schema = build_theme_schema(MoviesTheme)
    schema.metadata.create_all(engine)
    movies, cast = schema.entities[Movie].table, schema.relationships["cast"].table
    with engine.begin() as conn:
        conn.execute(
            insert(Entity),
            [
                {"id": 1, "type": "movies.actor", "title": "Sigourney Weaver"},
                {"id": 2, "type": "movies.movie", "title": "Alien"},
            ],
        )
        conn.execute(insert(schema.entities[Actor].table).values(id=1, born=date(1949, 10, 8)))
        conn.execute(insert(movies).values(id=2, year=1979))
        conn.execute(insert(cast).values(a_id=1, b_id=2))
        conn.execute(delete(Entity).where(Entity.id == 2))
        assert conn.execute(select(movies.c.id)).all() == []
        assert conn.execute(select(cast.c.a_id)).all() == []


def test_theme_rows_need_an_entity(engine: Engine) -> None:
    schema = build_theme_schema(MoviesTheme)
    schema.metadata.create_all(engine)
    with pytest.raises(exc.IntegrityError, match="FOREIGN KEY"), engine.begin() as conn:
        conn.execute(insert(schema.entities[Movie].table).values(id=99, year=2000))


class Song(ThemeEntity):
    title_label = "Title"
    track: int = field("Track", search="range")
    duration: float = field("Seconds")
    explicit: bool = field("Explicit", search="choice")
    lyrics: str = field("Lyrics", search="text")
    released: date | None = field("Released", search="range")
    added: datetime | None = field("Added")


class Band(ThemeEntity):
    name_note: str | None = field("Note")


class MusicTheme(Theme):
    id, name = "music", "Music"
    entities = [Song, Band]
    relationships = [related("performed_by", Song, Band, many=False)]


def test_column_types_and_defaults_for_missing_values(engine: Engine) -> None:
    schema = build_theme_schema(MusicTheme)
    schema.metadata.create_all(engine)
    songs = schema.entities[Song].table
    with engine.begin() as conn:
        conn.execute(insert(Entity).values(id=1, type="music.song", title="One"))
        conn.execute(insert(songs).values(id=1))  # ingest may omit non-nullable fields
        row = conn.execute(select(songs)).one()
    assert row._mapping == {
        "id": 1,
        "track": 0,
        "duration": 0.0,
        "explicit": False,
        "lyrics": "",
        "released": None,
        "added": None,
    }
    with engine.begin() as conn:
        added = datetime(2026, 9, 1, 12, tzinfo=UTC)
        conn.execute(songs.update().values(released=date(1991, 9, 24), added=added))
        assert conn.execute(select(songs.c.released, songs.c.added)).one() == (
            date(1991, 9, 24),
            added,
        )
    assert "ix_music_song_lyrics" not in {ix.name for ix in songs.indexes}  # text uses FTS


def test_many_false_allows_one_a_per_b(engine: Engine) -> None:
    schema = build_theme_schema(MusicTheme)
    schema.metadata.create_all(engine)
    link = schema.relationships["performed_by"].table
    with engine.begin() as conn:
        conn.execute(
            insert(Entity),
            [{"id": i, "type": "music.x", "title": str(i)} for i in (1, 2, 3)],
        )
        conn.execute(insert(link).values(a_id=1, b_id=3))
    with pytest.raises(exc.IntegrityError, match="UNIQUE"), engine.begin() as conn:
        conn.execute(insert(link).values(a_id=2, b_id=3))


def test_builds_are_isolated_and_repeatable() -> None:
    first, second = build_theme_schema(MoviesTheme), build_theme_schema(MoviesTheme)
    assert first.metadata is not second.metadata
    assert first.entities[Movie].table is not second.entities[Movie].table
    assert "movies_movie" not in Base.metadata.tables  # the core metadata is never touched


def test_every_problem_is_reported_at_once() -> None:
    class Clash(ThemeEntity):
        table_name = "bad_clash"
        type_id = "other.clash"
        id: int = field("Id")
        _hidden: str = field("Hidden")
        when: date = field("When")

    class Twin(ThemeEntity):
        table_name = "bad_clash"

    class Stranger(ThemeEntity):
        pass

    class Entities(ThemeEntity):
        table_name = "bad_entity"

    class BadTheme(Theme):
        id, name = "bad", "Bad"
        entities = [Clash, Twin, Entities]
        relationships = [
            related("knows", Clash, Stranger),
            related("clash", Clash, Twin),
        ]

    with pytest.raises(SchemaBuildError) as info:
        build_theme_schema(BadTheme)
    problems = "\n".join(info.value.problems)
    for expected in [
        "type id 'other.clash' must start with 'bad.'",
        "Clash.id: that field name is reserved",
        "Clash._hidden: that field name is reserved",
        "Clash.when: date and time fields must allow None",
        "Twin: table 'bad_clash' is also used by Clash",
        "relationship 'knows': Stranger is not one of the theme's entities",
        "relationship 'clash': table name 'bad_clash' is already used",
    ]:
        assert expected in problems
    assert str(info.value).startswith("Theme 'bad' has problems:")


@pytest.mark.parametrize("theme_id", ["Music", "my-theme", "", "1st"])
def test_theme_id_must_be_a_lowercase_identifier(theme_id: str) -> None:
    class T(Theme):
        name = "T"

    T.id = theme_id
    with pytest.raises(SchemaBuildError, match="lowercase identifier"):
        build_theme_schema(T)


def test_core_table_names_are_refused() -> None:
    class Thing(ThemeEntity):
        table_name = "entity_fts"

    class T(Theme):
        id, name = "entity", "E"
        entities = [Thing]

    with pytest.raises(SchemaBuildError) as info:
        build_theme_schema(T)
    assert any("used by the core" in p for p in info.value.problems)


def test_labels_must_be_non_empty_strings() -> None:
    class Blank(ThemeEntity):
        label = "  "

    class Wrong(ThemeEntity):
        plural = 3  # type: ignore[assignment]

    class T(Theme):
        id, name = "labels", "Labels"
        entities = [Blank, Wrong]

    with pytest.raises(SchemaBuildError) as info:
        build_theme_schema(T)
    assert info.value.problems == [
        "Blank: label must be a non-empty string or None",
        "Wrong: plural must be a non-empty string or None",
    ]
