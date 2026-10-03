"""Explicit theme schema changes: ``Theme.migrate_schema`` renaming, retyping, and dropping
fields during an upgrade (DESIGN.md §9 "Theme schema versions", #173)."""

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, Table, column, delete, insert, inspect, select, table

from tagalot.core import fts
from tagalot.core.db import open_keep_database
from tagalot.core.ingest import IngestSession, theme_text_source
from tagalot.core.keep import Keep, ThemeRef, create_keep, load_keep_config
from tagalot.core.models import Entity, FieldProvenance, FieldSource, SavedSearch, entity_fts
from tagalot.core.saved_searches import SavedDefinition
from tagalot.core.search_spec import RangeFilter, SearchSpec, SortKey
from tagalot.core.theme_db import ContextFactory, open_theme
from tagalot.core.theme_migrate import SchemaMigrationError, checked, convert_value
from tagalot.core.theme_schema import build_theme_schema
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import IngestContext, SchemaOps, Theme, field
from tagalot.themes.loader import validate_theme

# Version 1 of a theme, and what each test's version 2 starts from.


class Song1(ThemeEntity):
    table_name, type_id = "tunes_song", "tunes.song"
    tempo: int | None = field("Tempo", search="range")
    code: str | None = field("Code")
    notes: str | None = field("Notes", search="text")
    year: int | None = field("Year", search="choice")


class Tunes1(Theme):
    id, name, version = "tunes", "Tunes", 1
    entities = [Song1]


def tunes2(
    song: type[ThemeEntity],
    schema_ops: Callable[[SchemaOps], None],
    data: Callable[[IngestContext], None] | None = None,
) -> type[Theme]:
    """Version 2 with ``song``; ``schema_ops`` is its ``migrate_schema``, ``data`` its
    ``migrate``."""

    class Tunes2(Theme):
        id, name, version, api_version = "tunes", "Tunes", 2, 2
        entities = [song]

        def migrate_schema(self, from_version: int, ops: SchemaOps) -> None:
            assert from_version == 1
            schema_ops(ops)

        def migrate(self, from_version: int, ctx: IngestContext) -> None:
            if data is not None:
                data(ctx)

    return Tunes2


SONGS: list[dict[str, Any]] = [
    {"id": 1, "tempo": 120, "code": "120", "notes": "upbeat", "year": 1991},
    {"id": 2, "tempo": None, "code": " 7 ", "notes": None, "year": 1991},
    {"id": 3, "tempo": 90, "code": "", "notes": "slow", "year": None},
    {"id": 4, "tempo": 60, "code": None, "notes": None, "year": 2001},
]


@pytest.fixture
def keep(tmp_path: Path) -> Iterator[tuple[Keep, Engine]]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("tunes", 1)))
    open_theme(engine, keep, Tunes1)
    schema = build_theme_schema(Tunes1)
    with engine.begin() as conn:
        for song in SONGS:
            conn.execute(insert(Entity).values(id=song["id"], type="tunes.song", title="Song"))
        conn.execute(insert(schema.entities[Song1].table), SONGS)
        conn.execute(
            insert(FieldProvenance),
            [
                {"entity_id": 1, "field": "tempo", "source": FieldSource.USER},
                {"entity_id": 1, "field": "code", "source": FieldSource.USER},
                {"entity_id": 2, "field": "year", "source": FieldSource.EXTRACTED},
            ],
        )
        fts.sync_entities(conn, [s["id"] for s in SONGS], theme_text_source(schema))
    yield keep, engine
    engine.dispose()


def upgrade(keep: tuple[Keep, Engine], theme: type[Theme]) -> Any:
    k, engine = keep
    schema = build_theme_schema(theme)
    context: ContextFactory = lambda conn: IngestSession(conn, schema)  # noqa: E731
    return open_theme(engine, k, theme, allow_migration=True, make_context=context)


def song_table() -> Table:
    return build_theme_schema(Tunes1).entities[Song1].table


def rows(engine: Engine) -> list[dict[str, Any]]:
    """The song table's rows, in id order, as SQLite stores them."""
    with engine.connect() as conn:
        names = [c["name"] for c in inspect(conn).get_columns("tunes_song")]
        query: Any = select(*(column(n) for n in names)).select_from(table("tunes_song"))
        return [dict(r) for r in conn.execute(query).mappings()]


def columns(engine: Engine) -> list[str]:
    return [c["name"] for c in inspect(engine).get_columns("tunes_song")]


def indexes(engine: Engine) -> set[str]:
    return {str(ix["name"]) for ix in inspect(engine).get_indexes("tunes_song")}


def provenance(engine: Engine) -> set[tuple[int, str]]:
    with engine.connect() as conn:
        return {(r.entity_id, r.field) for r in conn.execute(select(FieldProvenance))}


# --- rename ---


class SongRenamed(ThemeEntity):
    table_name, type_id = "tunes_song", "tunes.song"
    bpm: int | None = field("BPM", search="range")
    code: str | None = field("Code")
    notes: str | None = field("Notes", search="text")
    year: int | None = field("Year", search="choice")


def test_rename_keeps_values_provenance_indexes_and_saved_searches(
    keep: tuple[Keep, Engine],
) -> None:
    k, engine = keep
    on_tunes = SearchSpec(
        types=("tunes.song",),
        fields=(RangeFilter("tempo", 100, None),),
        sort=(SortKey("tempo", True),),
    )
    elsewhere = SearchSpec(types=("tunes.album",), fields=(RangeFilter("tempo", 1, None),))
    with engine.begin() as conn:
        conn.execute(
            insert(SavedSearch),
            [
                {"id": 1, "name": "Fast", "definition": SavedDefinition(base=on_tunes).to_json()},
                {
                    "id": 2,
                    "name": "Bare",
                    "definition": SearchSpec(sort=(SortKey("tempo"),)).to_json(),
                },
                {"id": 3, "name": "Other", "definition": SavedDefinition(base=elsewhere).to_json()},
            ],
        )
    seen: list[Any] = []

    def data(ctx: IngestContext) -> None:  # migrate() runs after, and sees the new name
        seen.extend(ctx.get(ref).fields["bpm"] for ref in ctx.find(SongRenamed, bpm=120))

    opened = upgrade(
        keep, tunes2(SongRenamed, lambda ops: ops.rename_field(SongRenamed, "tempo", "bpm"), data)
    )

    assert columns(engine) == ["id", "bpm", "code", "notes", "year"]
    assert [r["bpm"] for r in rows(engine)] == [120, None, 90, 60]
    assert (1, "bpm") in provenance(engine)
    assert (1, "tempo") not in provenance(engine)
    assert "ix_tunes_song_bpm" in indexes(engine)
    assert "ix_tunes_song_tempo" not in indexes(engine)
    assert opened.changes.operations == ["tunes_song: tempo renamed to bpm (2 saved searches)"]
    assert seen == [120]
    with engine.connect() as conn:
        found = conn.execute(select(SavedSearch.id, SavedSearch.definition))
        saved: dict[int, Any] = {search_id: definition for search_id, definition in found}
    first = SavedDefinition.from_json(saved[1]).base
    assert first.fields == (RangeFilter("bpm", 100, None),)
    assert first.sort == (SortKey("bpm", True),)
    assert SearchSpec.from_json(saved[2]).sort == (SortKey("bpm"),)  # every type: renamed
    assert SavedDefinition.from_json(saved[3]).base.fields[0].field == "tempo"  # not songs
    assert load_keep_config(k.toml_path).theme.version == 2


def test_rename_of_a_field_the_keep_never_had_is_skipped(keep: tuple[Keep, Engine]) -> None:
    _, engine = keep

    def schema_ops(ops: SchemaOps) -> None:
        ops.rename_field(SongRenamed, "speed", "bpm")  # never existed: nothing to do
        ops.rename_field(SongRenamed, "tempo", "bpm")

    opened = upgrade(keep, tunes2(SongRenamed, schema_ops))
    assert opened.changes.operations == ["tunes_song: tempo renamed to bpm"]
    assert "bpm" in columns(engine)


def test_a_new_entity_types_fields_are_skipped(keep: tuple[Keep, Engine]) -> None:
    class Album(ThemeEntity):
        table_name, type_id = "tunes_album", "tunes.album"
        studio: str | None = field("Studio")

    class Tunes2(Theme):
        id, name, version, api_version = "tunes", "Tunes", 2, 2
        entities = [Song1, Album]

        def migrate_schema(self, from_version: int, ops: SchemaOps) -> None:
            ops.change_type(Album, "studio")  # its table comes with the additive step

    opened = upgrade(keep, Tunes2)
    assert opened.changes.operations == []
    assert opened.changes.tables == ["tunes_album"]


# --- change type ---


class SongCodeInt(ThemeEntity):
    table_name, type_id = "tunes_song", "tunes.song"
    tempo: int | None = field("Tempo", search="range")
    code: int | None = field("Code", search="range")
    notes: str | None = field("Notes", search="text")
    year: int | None = field("Year", search="choice")


def test_change_type_converts_values(keep: tuple[Keep, Engine]) -> None:
    _, engine = keep
    opened = upgrade(keep, tunes2(SongCodeInt, lambda ops: ops.change_type(SongCodeInt, "code")))
    assert [r["code"] for r in rows(engine)] == [120, 7, None, None]
    assert {c["name"]: str(c["type"]) for c in inspect(engine).get_columns("tunes_song")}[
        "code"
    ] == "INTEGER"
    assert columns(engine) == ["id", "tempo", "code", "notes", "year"]
    assert {"ix_tunes_song_tempo", "ix_tunes_song_year", "ix_tunes_song_code"} <= indexes(engine)
    assert provenance(engine) == {(1, "tempo"), (1, "code"), (2, "year")}
    assert opened.changes.operations == ["tunes_song: code is now int"]
    with engine.begin() as conn:  # the foreign key to entity survived the swap
        conn.execute(delete(Entity).where(Entity.id == 1))
    assert [r["id"] for r in rows(engine)] == [2, 3, 4]


class SongCodeRequired(ThemeEntity):
    table_name, type_id = "tunes_song", "tunes.song"
    code: int = field("Code")


def test_change_type_fills_defaults_when_none_isnt_allowed(keep: tuple[Keep, Engine]) -> None:
    _, engine = keep
    upgrade(keep, tunes2(SongCodeRequired, lambda ops: ops.change_type(SongCodeRequired, "code")))
    assert [r["code"] for r in rows(engine)] == [120, 7, 0, 0]


def test_change_type_with_the_themes_own_conversion(keep: tuple[Keep, Engine]) -> None:
    _, engine = keep

    def convert(value: Any) -> int | None:
        return None if value is None else len(value)

    upgrade(keep, tunes2(SongCodeInt, lambda ops: ops.change_type(SongCodeInt, "code", convert)))
    assert [r["code"] for r in rows(engine)] == [3, 3, 0, None]


class SongCodeWhen(ThemeEntity):
    table_name, type_id = "tunes_song", "tunes.song"
    code: datetime | None = field("Code")


def test_change_type_to_a_datetime_stores_utc(keep: tuple[Keep, Engine]) -> None:
    _, engine = keep
    with engine.begin() as conn:
        conn.execute(song_table().update().values(code="2024-01-02T03:04:05+02:00"))
    opened = upgrade(keep, tunes2(SongCodeWhen, lambda ops: ops.change_type(SongCodeWhen, "code")))
    with engine.connect() as conn:
        code = opened.schema.entities[SongCodeWhen].table.c.code
        values: list[Any] = list(conn.scalars(select(code)))
    assert set(values) == {datetime(2024, 1, 2, 1, 4, 5, tzinfo=UTC)}


def test_a_failed_conversion_changes_nothing(keep: tuple[Keep, Engine]) -> None:
    k, engine = keep
    song = song_table()
    with engine.begin() as conn:
        conn.execute(song.update().where(song.c.id == 3).values(code="fast"))

    def schema_ops(ops: SchemaOps) -> None:
        ops.rename_field(SongRenamedCodeInt, "tempo", "bpm")
        ops.change_type(SongRenamedCodeInt, "code")

    before = rows(engine)
    with pytest.raises(
        SchemaMigrationError,
        match=r"Couldn't upgrade this keep's 'Tunes' data to theme version 2: "
        r"SongRenamedCodeInt.code: changing its type: item 3's value 'fast' can't be "
        r"converted to int \(.*\)\. Nothing was changed; the backup made first is "
        r"keep\.db\.tunes-v1-",
    ):
        upgrade(keep, tunes2(SongRenamedCodeInt, schema_ops))
    assert rows(engine) == before  # the rename before it was rolled back too
    assert columns(engine) == ["id", "tempo", "code", "notes", "year"]
    assert "ix_tunes_song_tempo" in indexes(engine)
    assert "tunes_song__tagalot_new" not in inspect(engine).get_table_names()
    assert load_keep_config(k.toml_path).theme.version == 1
    assert len(list(k.dir.glob("keep.db.*.bak"))) == 1


class SongRenamedCodeInt(ThemeEntity):
    table_name, type_id = "tunes_song", "tunes.song"
    bpm: int | None = field("BPM", search="range")
    code: int | None = field("Code")
    notes: str | None = field("Notes", search="text")
    year: int | None = field("Year", search="choice")
    mood: str | None = field("Mood")  # new: added by the core after migrate_schema


def test_rename_retype_add_and_migrate_together(keep: tuple[Keep, Engine]) -> None:
    _, engine = keep

    def schema_ops(ops: SchemaOps) -> None:
        ops.rename_field(SongRenamedCodeInt, "tempo", "bpm")
        ops.change_type(SongRenamedCodeInt, "code", lambda v: None if v in (None, "") else int(v))

    def data(ctx: IngestContext) -> None:
        for ref in ctx.find(SongRenamedCodeInt):
            bpm = ctx.get(ref).fields["bpm"]
            ctx.update(ref, mood="fast" if bpm and bpm > 100 else "calm")

    upgrade(keep, tunes2(SongRenamedCodeInt, schema_ops, data))
    assert columns(engine) == ["id", "bpm", "code", "notes", "year", "mood"]
    assert [(r["bpm"], r["code"], r["mood"]) for r in rows(engine)] == [
        (120, 120, "fast"),
        (None, 7, "calm"),
        (90, None, "calm"),
        (60, None, "calm"),
    ]


# --- drop ---


class SongNoNotes(ThemeEntity):
    table_name, type_id = "tunes_song", "tunes.song"
    tempo: int | None = field("Tempo", search="range")
    year: int | None = field("Year", search="choice")


def fts_bodies(engine: Engine) -> dict[int, str]:
    with engine.connect() as conn:
        return {
            r.rowid: r.body for r in conn.execute(select(entity_fts.c.rowid, entity_fts.c.body))
        }


def test_drop_removes_the_column_its_provenance_and_its_text(keep: tuple[Keep, Engine]) -> None:
    _, engine = keep
    assert "upbeat" in fts_bodies(engine)[1]

    def schema_ops(ops: SchemaOps) -> None:
        ops.drop_field(SongNoNotes, "notes")
        ops.drop_field(SongNoNotes, "code")
        ops.drop_field(SongNoNotes, "lyrics")  # never existed: nothing to do

    opened = upgrade(keep, tunes2(SongNoNotes, schema_ops))
    assert columns(engine) == ["id", "tempo", "year"]
    assert [r["tempo"] for r in rows(engine)] == [120, None, 90, 60]
    assert provenance(engine) == {(1, "tempo"), (2, "year")}
    assert indexes(engine) == {"ix_tunes_song_tempo", "ix_tunes_song_year"}
    assert "upbeat" not in fts_bodies(engine)[1]
    assert opened.changes.operations == ["tunes_song: notes dropped", "tunes_song: code dropped"]


def test_without_a_drop_a_removed_field_stays(keep: tuple[Keep, Engine]) -> None:
    _, engine = keep
    upgrade(keep, tunes2(SongNoNotes, lambda ops: None))
    assert columns(engine) == ["id", "tempo", "code", "notes", "year"]


# --- mistakes in migrate_schema ---


class Stranger(ThemeEntity):
    table_name, type_id = "tunes_stranger", "tunes.stranger"


@pytest.mark.parametrize(
    ("schema_ops", "message"),
    [
        (
            lambda ops: ops.rename_field(SongRenamed, "tempo", "speed"),
            "'speed' isn't a field the theme declares",
        ),
        (
            lambda ops: ops.rename_field(SongRenamed, "code", "bpm"),
            "the theme still declares 'code'",
        ),
        (lambda ops: ops.drop_field(SongRenamed, "code"), "the theme still declares 'code'"),
        (
            lambda ops: ops.change_type(SongRenamed, "tempo"),
            "'tempo' isn't a field the theme declares",
        ),
        (lambda ops: ops.drop_field(Stranger, "x"), "Stranger is not one of the theme's entities"),
    ],
)
def test_mistakes_are_reported_and_change_nothing(
    keep: tuple[Keep, Engine], schema_ops: Callable[[SchemaOps], None], message: str
) -> None:
    _, engine = keep
    with pytest.raises(SchemaMigrationError, match=message):
        upgrade(keep, tunes2(SongRenamed, schema_ops))
    assert columns(engine) == ["id", "tempo", "code", "notes", "year"]


def test_renaming_onto_a_field_the_keep_has_is_refused(keep: tuple[Keep, Engine]) -> None:
    class SongBoth(ThemeEntity):
        table_name, type_id = "tunes_song", "tunes.song"
        year: int | None = field("Year")

    with pytest.raises(SchemaMigrationError, match="the keep already has both fields"):
        upgrade(keep, tunes2(SongBoth, lambda ops: ops.rename_field(SongBoth, "tempo", "year")))


def test_a_theme_with_migrate_schema_needs_api_version_2() -> None:
    problem = (
        "a theme with migrate_schema must set api_version = 2 (or later), so an older "
        "Tagalot refuses it rather than skip its schema changes"
    )

    class Old(Theme):
        id, name, api_version = "tunes", "Tunes", 1
        entities = [Song1]

        def migrate_schema(self, from_version: int, ops: SchemaOps) -> None:
            pass

    class Unsaid(Theme):  # api_version left out: an older Tagalot would take it as 1
        id, name = "tunes", "Tunes"
        entities = [Song1]

        def migrate_schema(self, from_version: int, ops: SchemaOps) -> None:
            pass

    assert problem in validate_theme(Old)
    assert problem in validate_theme(Unsaid)
    Old.api_version = 2
    assert validate_theme(Old) == []


# --- conversions ---


@pytest.mark.parametrize(
    ("raw", "target", "expected"),
    [
        ("120", int, 120),
        (" 7 ", int, 7),
        ("3.0", int, 3),
        (3.0, int, 3),
        ("", int, None),
        ("  ", str, None),
        (12, str, "12"),
        (1.5, str, "1.5"),
        ("1.5", float, 1.5),
        (2, float, 2.0),
        ("Yes", bool, True),
        ("off", bool, False),
        (1, bool, True),
        (0, bool, False),
        ("2024-05-06", date, date(2024, 5, 6)),
        ("2024-05-06 10:00:00.000000", date, date(2024, 5, 6)),
        ("2024-05-06 10:00:00", datetime, datetime(2024, 5, 6, 10, tzinfo=UTC)),
        (None, int, None),
    ],
)
def test_conversions(raw: Any, target: type, expected: Any) -> None:
    assert convert_value(raw, target) == expected


@pytest.mark.parametrize(
    ("raw", "target"),
    [
        ("fast", int),
        ("1.5", int),
        (1.5, int),
        ("maybe", bool),
        (2, bool),
        ("May", date),
        (3, date),
        (b"x", str),
    ],
)
def test_values_that_dont_convert(raw: Any, target: type) -> None:
    with pytest.raises(ValueError, match=r"."):
        convert_value(raw, target)


@pytest.mark.parametrize(
    ("value", "target", "nullable", "expected"),
    [
        (None, int, True, None),
        (None, int, False, 0),
        (None, str, False, ""),
        (3, float, True, 3.0),
        (datetime(2024, 1, 1), datetime, True, datetime(2024, 1, 1, tzinfo=UTC)),
    ],
)
def test_checked(value: Any, target: type, nullable: bool, expected: Any) -> None:
    assert checked(value, target, nullable) == expected


@pytest.mark.parametrize(
    ("value", "target"), [(True, int), ("1", int), (1.5, int), (datetime(2024, 1, 1), date)]
)
def test_checked_refuses_the_wrong_type(value: Any, target: type) -> None:
    with pytest.raises(ValueError, match=r"got a|date and time"):
        checked(value, target, True)
