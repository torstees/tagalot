"""Tests for theme fields in searches and result lists (core/search_fields.py)."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine

from tagalot.builtin_themes.generic import GenericTheme
from tagalot.core.db import open_keep_database
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.search import SearchError, choice_counts, count_matches, run_search
from tagalot.core.search_fields import (
    field_values,
    scope_fields,
    search_fields,
    type_labels,
    view_spec,
)
from tagalot.core.search_spec import (
    ChoiceFilter,
    RangeFilter,
    SearchSpec,
    SortKey,
    TextFilter,
)
from tagalot.core.tags import TagTree
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema, build_theme_schema
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import SearchView, SortBy, Theme, field


class Song(ThemeEntity):
    year: int | None = field("Year", search="range", card=True)
    genre: str | None = field("Genre", search="choice")
    length: int | None = field("Length")


class Video(ThemeEntity):
    length: int | None = field("Length")
    year: int | None = field("Year", search="range")


class Poster(ThemeEntity):
    year: str | None = field("Year")  # same name, different type


class Media(Theme):
    id, name = "media", "Media"
    entities = [Song, Video, Poster]


SONG, VIDEO, POSTER = "media.song", "media.video", "media.poster"


@dataclass
class Env:
    engine: Engine
    schema: ThemeSchema
    ids: dict[str, int]

    def titles(self, spec: SearchSpec) -> list[str]:
        fields = search_fields(self.schema, spec.types)
        with self.engine.connect() as conn:
            return [h.title for h in run_search(conn, spec, TagTree([], {}), fields=fields)]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("media", 1)))
    schema = open_theme(engine, keep, Media).schema
    ids: dict[str, int] = {}
    with engine.begin() as conn:
        ctx = IngestSession(conn, schema)
        rows: list[tuple[type[ThemeEntity], str, dict[str, object]]] = [
            (Song, "Alpha", {"year": 1999, "genre": "rock", "length": 200}),
            (Song, "Bravo", {"year": 1985, "genre": "jazz", "length": 300}),
            (Song, "Charlie", {"year": None, "genre": None, "length": 100}),
            (Video, "Delta", {"year": 2001, "length": 5000}),
            (Poster, "Echo", {"year": "MCMXC"}),
        ]
        for entity, title, values in rows:
            ids[title] = ctx.upsert(entity, title, title=title, **values).id
        ctx.flush()
    try:
        yield Env(engine, schema, ids)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("types", "expected"),
    [
        ((SONG,), ["year", "genre", "length"]),
        ((SONG, VIDEO), ["year", "length"]),  # the first type's order
        ((VIDEO, SONG), ["length", "year"]),
        ((SONG, VIDEO, POSTER), []),  # Poster's year is text, and it has no length
        ((), []),  # everything: Poster again
        (("media.gone",), []),
    ],
)
def test_scope_fields(env: Env, types: tuple[str, ...], expected: list[str]) -> None:
    assert [f.name for f in scope_fields(env.schema, types)] == expected


def test_sort_by_a_theme_field(env: Env) -> None:
    spec = SearchSpec(types=(SONG,), sort=(SortKey("year", descending=True),))
    # NULL sorts first ascending, so last descending; ties would fall back to the id.
    assert env.titles(spec) == ["Alpha", "Bravo", "Charlie"]


def test_sort_across_types_uses_each_types_value(env: Env) -> None:
    spec = SearchSpec(types=(SONG, VIDEO), sort=(SortKey("length"),))
    assert env.titles(spec) == ["Charlie", "Alpha", "Bravo", "Delta"]


def test_filter_on_a_theme_field(env: Env) -> None:
    spec = SearchSpec(types=(SONG, VIDEO), fields=(RangeFilter("year", 1990, None),))
    assert env.titles(spec) == ["Alpha", "Delta"]
    spec = SearchSpec(types=(SONG,), fields=(TextFilter("genre", "JA"),))
    assert env.titles(spec) == ["Bravo"]
    with env.engine.connect() as conn:
        fields = search_fields(env.schema, spec.types)
        assert count_matches(conn, spec, TagTree([], {}), fields) == 1


def test_a_field_outside_the_scope_is_an_error(env: Env) -> None:
    spec = SearchSpec(types=(SONG, VIDEO), sort=(SortKey("genre"),))  # Video has no genre
    with pytest.raises(SearchError, match="genre"):
        env.titles(spec)


def test_field_values(env: Env) -> None:
    with env.engine.connect() as conn:
        hits = run_search(conn, SearchSpec(), TagTree([], {}))
        values = field_values(conn, env.schema, hits, ["year", "genre"])
    ids = env.ids
    assert values[ids["Alpha"]] == {"year": 1999, "genre": "rock"}
    assert values[ids["Charlie"]] == {"year": None, "genre": None}
    assert values[ids["Delta"]] == {"year": 2001}  # videos have no genre
    assert values[ids["Echo"]] == {"year": "MCMXC"}


def test_view_spec_and_labels() -> None:
    schema = build_theme_schema(GenericTheme)
    [files, recent] = [v for v in GenericTheme.views if isinstance(v, SearchView)]
    assert view_spec(schema, files) == SearchSpec(types=("generic.file",))
    spec = view_spec(schema, recent)
    assert spec.sort == (SortKey("modified", descending=True),)
    assert type_labels(schema) == {"generic.file": "File"}
    fields = [f.name for f in scope_fields(schema, spec.types)]
    assert fields == ["extension", "folder", "size", "modified"]
    assert SortBy("modified", True).field in search_fields(schema, spec.types)


def test_datetime_values_round_trip(tmp_path: Path) -> None:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("generic", 1)))
    try:
        schema = open_theme(engine, keep, GenericTheme).schema
        when = datetime(2026, 9, 27, 12, 30, tzinfo=UTC)
        with engine.begin() as conn:
            ctx = IngestSession(conn, schema)
            [file_entity] = schema.entities
            ctx.upsert(file_entity, "a", title="a.txt", extension="txt", folder="", modified=when)
            ctx.flush()
        with engine.connect() as conn:
            hits = run_search(conn, SearchSpec(), TagTree([], {}))
            [values] = field_values(conn, schema, hits, ["modified", "extension"]).values()
        assert values["extension"] == "txt"
        assert values["modified"] == when
    finally:
        engine.dispose()


# --- choice counts, for the field filter popup (#94) ---


def _counts(env: Env, spec: SearchSpec, name: str) -> list[tuple[object, int]]:
    with env.engine.connect() as conn:
        return choice_counts(
            conn, spec, TagTree([], {}), name, search_fields(env.schema, spec.types)
        )


def test_choice_counts_among_the_results(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = IngestSession(conn, env.schema)
        ctx.upsert(Song, "Foxtrot", title="Foxtrot", year=1999, genre="rock")
        ctx.flush()
    songs = SearchSpec(types=(SONG,))
    assert _counts(env, songs, "genre") == [("rock", 2), ("jazz", 1)]  # most first; no NULL
    older = SearchSpec(types=(SONG,), fields=(RangeFilter("year", None, 1990),))
    assert _counts(env, older, "genre") == [("jazz", 1)]  # only among the results
    assert _counts(env, SearchSpec(types=(SONG, VIDEO)), "year") == [
        (1999, 2),
        (1985, 1),
        (2001, 1),
    ]


def test_choice_counts_ignore_the_fields_own_filter(env: Env) -> None:
    spec = SearchSpec(types=(SONG,), fields=(ChoiceFilter("genre", ("rock",)),))
    assert env.titles(spec) == ["Alpha"]
    assert _counts(env, spec, "genre") == [("jazz", 1), ("rock", 1)]  # other values stay


def test_choice_counts_of_an_unknown_field(env: Env) -> None:
    with pytest.raises(SearchError, match="length"):
        _counts(env, SearchSpec(types=(SONG, VIDEO, POSTER)), "length")
