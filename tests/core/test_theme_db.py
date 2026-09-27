"""Tests for creating, checking, and migrating a keep's theme tables."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Connection, Engine, Table, insert, inspect, select, update

from tagalot.core.db import KeepNeedsMigration, KeepVersionError, open_keep_database
from tagalot.core.keep import Keep, ThemeRef, create_keep, load_keep_config
from tagalot.core.models import Entity, SchemaVersion
from tagalot.core.theme_db import ContextFactory, KeepThemeError, open_theme
from tagalot.core.theme_schema import build_theme_schema
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import IngestContext, Theme, field


class SongV1(ThemeEntity):
    table_name, type_id = "tunes_song", "tunes.song"
    year: int | None = field("Year", search="range")


class TunesV1(Theme):
    id, name, version = "tunes", "Tunes", 1
    entities = [SongV1]


class SongV2(ThemeEntity):
    table_name, type_id = "tunes_song", "tunes.song"
    year: int | None = field("Year", search="range")
    bpm: int = field("BPM", search="range")  # new, non-nullable: needs a default
    mood: str | None = field("Mood")  # new, nullable


class ArtistV2(ThemeEntity):
    table_name, type_id = "tunes_artist", "tunes.artist"
    country: str | None = field("Country", search="choice")


migrations: list[tuple[int, object]] = []


class TunesV2(Theme):
    id, name, version = "tunes", "Tunes", 2
    entities = [SongV2, ArtistV2]

    def migrate(self, from_version: int, ctx: Any) -> None:
        migrations.append((from_version, ctx))
        if getattr(ctx, "fail", False):
            raise RuntimeError("migration failed halfway")


class StubContext:
    def __init__(self, conn: Connection, fail: bool = False) -> None:
        self.conn = conn
        self.fail = fail


@pytest.fixture
def keep(tmp_path: Path) -> Iterator[tuple[Keep, Engine]]:
    migrations.clear()
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("tunes", 1)))
    yield keep, engine
    engine.dispose()


def _stored(engine: Engine) -> int | None:
    with engine.connect() as conn:
        return conn.scalar(select(SchemaVersion.version).where(SchemaVersion.component == "tunes"))


def _columns(engine: Engine, table: str) -> list[str]:
    return [c["name"] for c in inspect(engine).get_columns(table)]


def _backups(keep: Keep) -> list[Path]:
    return sorted(keep.dir.glob("keep.db.*.bak"))


def _add_song(engine: Engine, year: int) -> None:
    with engine.begin() as conn:
        conn.execute(insert(Entity).values(id=1, type="tunes.song", title="Song"))
        conn.execute(insert(_song_table_v1()).values(id=1, year=year))


def _song_table_v1() -> Table:
    return build_theme_schema(TunesV1).entities[SongV1].table


def _stub(fail: bool = False) -> ContextFactory:
    return lambda conn: cast(IngestContext, StubContext(conn, fail=fail))


def test_new_keep_gets_tables_and_version(keep: tuple[Keep, Engine]) -> None:
    k, engine = keep
    opened = open_theme(engine, k, TunesV1)
    assert "tunes_song" in inspect(engine).get_table_names()
    assert _stored(engine) == 1
    assert opened.changes.tables == ["tunes_song"]
    assert opened.migrated_from is None


def test_reopening_changes_nothing(keep: tuple[Keep, Engine]) -> None:
    k, engine = keep
    open_theme(engine, k, TunesV1)
    toml = k.toml_path.read_bytes()
    again = open_theme(engine, k, TunesV1)
    assert not again.changes
    assert k.toml_path.read_bytes() == toml
    assert _backups(k) == []


def test_wrong_theme(keep: tuple[Keep, Engine]) -> None:
    k, engine = keep

    class Other(Theme):
        id, name = "other", "Other"
        entities = [SongV1]

    with pytest.raises(KeepThemeError, match="uses the 'tunes' theme, not 'other'"):
        open_theme(engine, k, Other)


def test_a_newer_keep_toml_is_refused_before_touching_the_database(
    keep: tuple[Keep, Engine],
) -> None:
    k, engine = keep
    newer = replace(k, config=replace(k.config, theme=ThemeRef("tunes", 5)))
    with pytest.raises(KeepVersionError, match=r"keep\.toml says 5; installed: 1"):
        open_theme(engine, newer, TunesV1)
    assert "tunes_song" not in inspect(engine).get_table_names()


def test_a_newer_database_is_refused(keep: tuple[Keep, Engine]) -> None:
    k, engine = keep
    open_theme(engine, k, TunesV1)
    with engine.begin() as conn:
        conn.execute(
            update(SchemaVersion).where(SchemaVersion.component == "tunes").values(version=3)
        )
    with pytest.raises(KeepVersionError, match="database has 3; installed: 1"):
        open_theme(engine, k, TunesV1)


def test_an_older_keep_needs_confirmation(keep: tuple[Keep, Engine]) -> None:
    k, engine = keep
    open_theme(engine, k, TunesV1)
    with pytest.raises(KeepNeedsMigration) as info:
        open_theme(engine, k, TunesV2)
    assert (info.value.stored, info.value.current, info.value.component) == (1, 2, "tunes")
    assert "tunes_artist" not in inspect(engine).get_table_names()  # nothing changed
    assert _backups(k) == []


def test_confirmed_migration_backs_up_adds_schema_and_runs_migrate(
    keep: tuple[Keep, Engine],
) -> None:
    k, engine = keep
    open_theme(engine, k, TunesV1)
    _add_song(engine, 1991)

    opened = open_theme(engine, k, TunesV2, allow_migration=True, make_context=_stub())
    assert opened.migrated_from == 1
    assert [v for v, _ in migrations] == [1]
    assert isinstance(migrations[0][1], StubContext)
    assert _stored(engine) == 2
    assert load_keep_config(k.toml_path).theme.version == 2
    assert opened.keep.config.theme.version == 2

    assert set(opened.changes.tables) == {"tunes_artist"}
    assert set(opened.changes.columns) == {"tunes_song.bpm", "tunes_song.mood"}
    assert "ix_tunes_song_bpm" in opened.changes.indexes
    assert _columns(engine, "tunes_song") == ["id", "year", "bpm", "mood"]
    with engine.connect() as conn:
        row = conn.execute(select(opened.schema.entities[SongV2].table)).one()
    assert row._mapping == {"id": 1, "year": 1991, "bpm": 0, "mood": None}  # data kept

    [backup] = _backups(k)
    assert backup.name.startswith("keep.db.tunes-v1-")


def test_failed_migration_rolls_back_everything(keep: tuple[Keep, Engine]) -> None:
    k, engine = keep
    open_theme(engine, k, TunesV1)
    with pytest.raises(RuntimeError, match="halfway"):
        open_theme(
            engine,
            k,
            TunesV2,
            allow_migration=True,
            make_context=_stub(fail=True),
        )
    assert _stored(engine) == 1
    assert load_keep_config(k.toml_path).theme.version == 1
    assert "tunes_artist" not in inspect(engine).get_table_names()  # DDL rolled back too
    assert _columns(engine, "tunes_song") == ["id", "year"]
    assert len(_backups(k)) == 1  # kept for the user


def test_migration_needs_a_context(keep: tuple[Keep, Engine]) -> None:
    k, engine = keep
    open_theme(engine, k, TunesV1)
    with pytest.raises(KeepThemeError, match="no ingest context"):
        open_theme(engine, k, TunesV2, allow_migration=True)
    assert _stored(engine) == 1


def test_additive_changes_without_a_version_bump(keep: tuple[Keep, Engine]) -> None:
    # A theme author adds a field but forgets to bump the version: the keep still works.
    k, engine = keep
    open_theme(engine, k, TunesV1)

    class TunesEdited(Theme):
        id, name, version = "tunes", "Tunes", 1
        entities = [SongV2]

    opened = open_theme(engine, k, TunesEdited)
    assert set(opened.changes.columns) == {"tunes_song.bpm", "tunes_song.mood"}
    assert opened.migrated_from is None
    assert _stored(engine) == 1


def test_a_broken_theme_is_a_keep_theme_error(keep: tuple[Keep, Engine]) -> None:
    k, engine = keep

    class Bad(ThemeEntity):
        when: date = field("When")

    class Broken(Theme):
        id, name = "tunes", "Tunes"
        entities = [Bad]

    with pytest.raises(KeepThemeError, match="date and time fields must allow None"):
        open_theme(engine, k, Broken)
