"""Tests for creating the core schema and checking its version when a keep opens."""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, inspect, select, text, update

from tagalot.core.db import (
    CORE,
    KeepNeedsMigration,
    KeepVersionError,
    create_keep_engine,
    open_keep_database,
)
from tagalot.core.keep import (
    KEEP_FORMAT_VERSION,
    Keep,
    KeepError,
    ThemeRef,
    create_keep,
    load_keep_config,
    open_keep,
    save_keep_config,
)
from tagalot.core.models import Base, SchemaVersion


def _keep(tmp_path: Path) -> Keep:
    return create_keep(tmp_path / "k", "K", ThemeRef(id="generic", version=1))


def _open_at_v1(tmp_path: Path) -> tuple[Keep, Engine]:
    """A keep created at core format 1, so the migration machinery can be tested with fake
    steps whatever the current format is."""
    keep = _keep(tmp_path)
    keep.config.format_version = 1
    save_keep_config(keep.config, keep.toml_path)
    return open_keep_database(open_keep(keep.dir), current_version=1)


def _stored_version(keep: Keep) -> int | None:
    conn = sqlite3.connect(keep.db_path)
    try:
        row = conn.execute("SELECT version FROM schema_version WHERE component = 'core'").fetchone()
    finally:
        conn.close()
    return None if row is None else int(row[0])


def _set_db_version(keep: Keep, version: int) -> None:
    engine = create_keep_engine(keep.db_path)
    with engine.begin() as conn:
        conn.execute(update(SchemaVersion).values(version=version))
    engine.dispose()


def _add_column(conn: Connection) -> None:
    conn.exec_driver_sql("ALTER TABLE root ADD COLUMN note TEXT")


def _backups(keep: Keep) -> list[Path]:
    return sorted(keep.dir.glob("keep.db.*.bak"))


def test_first_open_creates_schema_and_version(tmp_path: Path) -> None:
    _, engine = open_keep_database(_keep(tmp_path))
    try:
        tables = set(inspect(engine).get_table_names())
        assert set(Base.metadata.tables) | {"entity_fts"} <= tables
        with engine.connect() as conn:
            rows = conn.execute(select(SchemaVersion.component, SchemaVersion.version)).all()
        assert rows == [(CORE, KEEP_FORMAT_VERSION)]
    finally:
        engine.dispose()


def test_reopening_a_current_keep_changes_nothing(tmp_path: Path) -> None:
    keep, engine = open_keep_database(_keep(tmp_path))
    engine.dispose()
    toml_before = keep.toml_path.read_bytes()

    reopened, engine = open_keep_database(open_keep(keep.dir))
    engine.dispose()
    assert reopened.config == keep.config
    assert keep.toml_path.read_bytes() == toml_before
    assert _backups(keep) == []


def test_newer_keep_toml_is_refused_before_touching_the_database(tmp_path: Path) -> None:
    keep = _keep(tmp_path)
    keep.config.format_version = KEEP_FORMAT_VERSION + 1
    save_keep_config(keep.config, keep.toml_path)
    newer = KEEP_FORMAT_VERSION + 1
    with pytest.raises(KeepVersionError, match=rf"newer version of Tagalot.*keep format {newer}"):
        open_keep_database(open_keep(keep.dir))
    assert not keep.db_path.exists()


def test_newer_database_is_refused(tmp_path: Path) -> None:
    keep, engine = open_keep_database(_keep(tmp_path))
    engine.dispose()
    _set_db_version(keep, 99)
    with pytest.raises(KeepVersionError, match="database schema 99"):
        open_keep_database(keep)
    assert _stored_version(keep) == 99


def test_older_keep_needs_confirmation(tmp_path: Path) -> None:
    keep, engine = _open_at_v1(tmp_path)
    engine.dispose()
    with pytest.raises(KeepNeedsMigration) as info:
        open_keep_database(keep, current_version=2, migrations={1: _add_column})
    assert (info.value.stored, info.value.current) == (1, 2)
    assert isinstance(info.value, KeepError)  # the launcher can catch KeepError
    assert _stored_version(keep) == 1
    assert _backups(keep) == []


def test_confirmed_migration_backs_up_then_upgrades(tmp_path: Path) -> None:
    keep, engine = _open_at_v1(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO root (id, name, online) VALUES ('r1', 'Files', 1)")
    engine.dispose()

    migrated, engine = open_keep_database(
        keep, allow_migration=True, current_version=2, migrations={1: _add_column}
    )
    try:
        assert "note" in {c["name"] for c in inspect(engine).get_columns("root")}
    finally:
        engine.dispose()
    assert _stored_version(keep) == 2
    assert migrated.config.format_version == 2
    assert load_keep_config(keep.toml_path).format_version == 2

    [backup] = _backups(keep)
    assert backup.name.startswith("keep.db.v1-")
    raw = sqlite3.connect(backup)
    try:
        assert raw.execute("SELECT version FROM schema_version").fetchone() == (1,)
        assert raw.execute("SELECT id FROM root").fetchall() == [("r1",)]  # committed data kept
        assert "note" not in {r[1] for r in raw.execute("PRAGMA table_info(root)")}
    finally:
        raw.close()


def test_multi_step_migration_runs_every_step_in_order(tmp_path: Path) -> None:
    keep, engine = _open_at_v1(tmp_path)
    engine.dispose()
    ran: list[int] = []
    steps = {v: (lambda conn, v=v: ran.append(v)) for v in (1, 2, 3)}
    open_keep_database(keep, allow_migration=True, current_version=4, migrations=steps)[1].dispose()
    assert ran == [1, 2, 3]
    assert _stored_version(keep) == 4


def test_failed_migration_rolls_back_completely(tmp_path: Path) -> None:
    keep, engine = _open_at_v1(tmp_path)
    engine.dispose()

    def half_done(conn: Connection) -> None:
        _add_column(conn)
        raise RuntimeError("step failed")

    with pytest.raises(RuntimeError, match="step failed"):
        open_keep_database(keep, allow_migration=True, current_version=2, migrations={1: half_done})
    assert _stored_version(keep) == 1
    assert load_keep_config(keep.toml_path).format_version == 1
    engine = create_keep_engine(keep.db_path)
    try:
        assert "note" not in {c["name"] for c in inspect(engine).get_columns("root")}
    finally:
        engine.dispose()
    assert len(_backups(keep)) == 1  # the backup is kept for the user


def test_missing_migration_step_is_reported(tmp_path: Path) -> None:
    keep, engine = _open_at_v1(tmp_path)
    engine.dispose()
    with pytest.raises(KeepVersionError, match="No upgrade path from database format 1 to 3"):
        open_keep_database(
            keep, allow_migration=True, current_version=3, migrations={1: _add_column}
        )
    assert _stored_version(keep) == 1


def test_database_without_version_table_is_reported(tmp_path: Path) -> None:
    keep = _keep(tmp_path)
    conn = sqlite3.connect(keep.db_path)
    conn.execute("CREATE TABLE something_else (x)")
    conn.close()
    with pytest.raises(KeepVersionError, match="no schema version"):
        open_keep_database(keep)


def test_database_without_core_row_is_reported(tmp_path: Path) -> None:
    keep, engine = open_keep_database(_keep(tmp_path))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM schema_version"))
    engine.dispose()
    with pytest.raises(KeepVersionError, match="no core schema version"):
        open_keep_database(keep)


def test_format_1_keeps_upgrade_through_every_step(tmp_path: Path) -> None:
    """The real steps: 1 -> 2 gives tags a description (#192), 2 -> 3 gives roots the
    options they were ingested with (#82), 3 -> 4 adds triage dismissals (#110), 4 -> 5
    flags skipped files (#152), 5 -> 6 marks links made by hand (#243); nothing else
    changes."""
    keep, engine = open_keep_database(_keep(tmp_path))
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO tag (id, name, sort_order) VALUES (1, 'Iceland', 0)")
        conn.exec_driver_sql("INSERT INTO root (id, name, online) VALUES ('r', 'Photos', 0)")
        # Put the database back the way format 1 made it.
        conn.exec_driver_sql("ALTER TABLE tag DROP COLUMN description")
        conn.exec_driver_sql("ALTER TABLE tag DROP COLUMN types")
        conn.exec_driver_sql("ALTER TABLE entity_tag DROP COLUMN by_file")
        for table in ("entity_keyword", "keyword_ignored", "file_tag_removal"):
            conn.exec_driver_sql(f"DROP TABLE {table}")
        conn.exec_driver_sql("ALTER TABLE root DROP COLUMN ingest_options")
        conn.exec_driver_sql("DROP TABLE triage_dismissal")
        conn.exec_driver_sql("ALTER TABLE resource DROP COLUMN skipped")
        conn.exec_driver_sql("ALTER TABLE entity_resource DROP COLUMN by_user")
        conn.exec_driver_sql("DROP TABLE entity_merge_resource")
        conn.exec_driver_sql("DROP TABLE entity_merge")
        conn.exec_driver_sql("DROP TABLE dedupe_dismissal")
        conn.exec_driver_sql("DROP TABLE user_relation")
        conn.exec_driver_sql("DROP TABLE user_order")
        conn.execute(update(SchemaVersion).values(version=1))
    engine.dispose()
    keep.config.format_version = 1
    save_keep_config(keep.config, keep.toml_path)

    with pytest.raises(KeepNeedsMigration) as info:
        open_keep_database(open_keep(keep.dir))
    assert (info.value.stored, info.value.current) == (1, 12)

    migrated, engine = open_keep_database(open_keep(keep.dir), allow_migration=True)
    try:
        tag_columns = {c["name"] for c in inspect(engine).get_columns("tag")}
        assert {"description", "types"} <= tag_columns  # format 10 limits tags' types (#135)
        assert "ingest_options" in {c["name"] for c in inspect(engine).get_columns("root")}
        assert "triage_dismissal" in inspect(engine).get_table_names()
        assert "skipped" in {c["name"] for c in inspect(engine).get_columns("resource")}
        links = {c["name"] for c in inspect(engine).get_columns("entity_resource")}
        assert "by_user" in links
        assert "entity_merge" in inspect(engine).get_table_names()
        assert "dedupe_dismissal" in inspect(engine).get_table_names()
        assert "user_relation" in inspect(engine).get_table_names()
        assert {"entity_keyword", "keyword_ignored", "file_tag_removal"} <= set(
            inspect(engine).get_table_names()
        )  # format 11: keywords from files (#294)
        assert "by_file" in {c["name"] for c in inspect(engine).get_columns("entity_tag")}
        assert "user_order" in inspect(engine).get_table_names()  # format 12 (#317)
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, name, description, types FROM tag")).all()
            roots = conn.execute(text("SELECT id, name, ingest_options FROM root")).all()
        assert [tuple(r) for r in rows] == [(1, "Iceland", None, None)]
        assert [tuple(r) for r in roots] == [("r", "Photos", None)]
    finally:
        engine.dispose()
    assert migrated.config.format_version == 12
    assert [b.name.startswith("keep.db.v1-") for b in _backups(keep)] == [True]
