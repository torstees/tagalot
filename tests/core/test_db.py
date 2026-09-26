"""Tests for keep database engine setup."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    Engine,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    exc,
    insert,
    inspect,
    select,
    text,
)

from tagalot.core.db import BUSY_TIMEOUT_MS, create_keep_engine, open_keep_engine
from tagalot.core.keep import Keep, ThemeRef, create_keep

metadata = MetaData()
parent = Table("parent", metadata, Column("id", Integer, primary_key=True))
child = Table(
    "child",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("parent_id", ForeignKey("parent.id"), nullable=False),
    Column("name", String),
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def _pragma(engine: Engine, name: str) -> object:
    with engine.connect() as conn:
        return conn.exec_driver_sql(f"PRAGMA {name}").scalar()


def test_local_keep_pragmas(engine: Engine) -> None:
    assert _pragma(engine, "foreign_keys") == 1
    assert _pragma(engine, "busy_timeout") == BUSY_TIMEOUT_MS
    assert _pragma(engine, "journal_mode") == "wal"
    assert _pragma(engine, "synchronous") == 1  # NORMAL
    assert _pragma(engine, "query_only") == 0


def test_network_keep_uses_rollback_journal(tmp_path: Path) -> None:
    engine = create_keep_engine(tmp_path / "keep.db", network=True)
    try:
        assert _pragma(engine, "journal_mode") == "delete"
        assert _pragma(engine, "synchronous") == 2  # FULL
        assert _pragma(engine, "foreign_keys") == 1
    finally:
        engine.dispose()


def test_foreign_keys_are_enforced(engine: Engine) -> None:
    with pytest.raises(exc.IntegrityError, match="FOREIGN KEY"), engine.begin() as conn:
        conn.execute(insert(child).values(id=1, parent_id=99))


def test_rollback_undoes_ddl(engine: Engine) -> None:
    extra = Table("extra", MetaData(), Column("id", Integer, primary_key=True))
    with engine.connect() as conn:
        trans = conn.begin()
        extra.create(conn)
        assert inspect(conn).has_table("extra")
        trans.rollback()
    assert not inspect(engine).has_table("extra")


def test_savepoint_rolls_back_only_inner_work(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(insert(parent).values(id=1))
        nested = conn.begin_nested()
        conn.execute(insert(parent).values(id=2))
        nested.rollback()
    with engine.connect() as conn:
        assert conn.execute(select(parent.c.id)).scalars().all() == [1]


def test_read_only_engine_refuses_writes(engine: Engine, tmp_path: Path) -> None:
    reader = create_keep_engine(tmp_path / "keep.db", read_only=True)
    try:
        assert _pragma(reader, "query_only") == 1
        with pytest.raises(exc.OperationalError, match="readonly"), reader.begin() as conn:
            conn.execute(insert(parent).values(id=1))
    finally:
        reader.dispose()


def test_reader_is_not_blocked_by_open_write_transaction(engine: Engine, tmp_path: Path) -> None:
    with engine.begin() as conn:
        conn.execute(insert(parent).values(id=1))
    reader = create_keep_engine(tmp_path / "keep.db", read_only=True)
    try:
        with engine.begin() as writer:
            writer.execute(insert(parent).values(id=2))  # uncommitted, holds the write lock
            with reader.connect() as conn:
                assert conn.execute(select(parent.c.id)).scalars().all() == [1]
    finally:
        reader.dispose()


def test_engine_is_usable_from_worker_threads(engine: Engine) -> None:
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            with engine.begin() as conn:
                conn.execute(insert(parent).values(id=n))
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(1, 6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM parent")).scalar() == 5


def test_unusual_characters_in_path(tmp_path: Path) -> None:
    folder = tmp_path / "Música & Art #1 %20"
    folder.mkdir()
    engine = create_keep_engine(folder / "keep.db")
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    assert (folder / "keep.db").is_file()


def test_open_keep_engine_uses_keep_location(tmp_path: Path) -> None:
    keep = create_keep(tmp_path / "k", "K", ThemeRef(id="generic", version=1))
    engine = open_keep_engine(keep)
    try:
        assert _pragma(engine, "journal_mode") == "wal"
    finally:
        engine.dispose()
    assert keep.db_path.is_file()

    network_keep = Keep(dir=keep.dir, config=keep.config, on_network=True)
    engine = open_keep_engine(network_keep)
    try:
        assert _pragma(engine, "journal_mode") == "delete"
    finally:
        engine.dispose()
