"""Tests for keeping entity_fts in sync and rebuilding it."""

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, delete, insert, select, update

from tagalot.core.db import create_keep_engine
from tagalot.core.fts import (
    fts_document,
    index_is_consistent,
    rebuild_search_index,
    sync_entities,
)
from tagalot.core.models import Base, Entity, entity_fts
from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tags import TagTree


@pytest.mark.parametrize(
    ("extra", "texts", "body"),
    [
        ({}, [], ""),
        ({"notes": "café ♪"}, [], "café ♪"),
        ({"rating": 4, "bpm": 120.5, "live": True, "gone": None}, [], "4 120.5"),
        (
            {"credits": ["Brian Eno", {"role": "producer", "name": "Flood"}]},
            [],
            "Brian Eno producer Flood",
        ),
        ({"notes": "b"}, ["Iceland", None, ""], "Iceland b"),
        (None, ["x"], "x"),
    ],
)
def test_document(extra: Any, texts: list[str | None], body: str) -> None:
    assert fts_document("Homogenic", extra, texts) == ("Homogenic", body)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(Entity),
            [
                {"id": 1, "type": "t", "title": "Abbey Road", "extra": {"notes": "remastered"}},
                {"id": 2, "type": "t", "title": "Homogenic", "extra": {}},
                {
                    "id": 3,
                    "type": "t",
                    "title": "Kind of Blue",
                    "extra": {"mood": ["calm", "late night"]},
                },
            ],
        )
    yield engine
    engine.dispose()


def _rows(engine: Engine) -> dict[int, tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(select(entity_fts.c.rowid, entity_fts.c.title, entity_fts.c.body))
        return {r: (t, b) for r, t, b in rows}


def _search(engine: Engine, text: str) -> list[int]:
    with engine.connect() as conn:
        return [h.id for h in run_search(conn, SearchSpec(text=text), TagTree([], {}))]


def test_sync_indexes_title_and_extra(engine: Engine) -> None:
    with engine.begin() as conn:
        sync_entities(conn, [1, 2, 3])
    assert _rows(engine) == {
        1: ("Abbey Road", "remastered"),
        2: ("Homogenic", ""),
        3: ("Kind of Blue", "calm late night"),
    }
    assert _search(engine, "night") == [3]
    with engine.connect() as conn:
        assert index_is_consistent(conn)


def test_sync_after_edits(engine: Engine) -> None:
    with engine.begin() as conn:
        sync_entities(conn, [1, 2, 3])
        conn.execute(update(Entity).where(Entity.id == 2).values(title="Homogenic (Deluxe)"))
        conn.execute(update(Entity).where(Entity.id == 1).values(extra={"notes": "mono mix"}))
    with engine.connect() as conn:
        assert not index_is_consistent(conn)  # stale until synced
    assert _search(engine, "remastered") == [1]

    with engine.begin() as conn:
        sync_entities(conn, [1, 2])
    assert _search(engine, "remastered") == []
    assert _search(engine, "mono") == [1]
    assert _search(engine, "deluxe") == [2]
    with engine.connect() as conn:
        assert index_is_consistent(conn)


def test_sync_removes_rows_of_deleted_entities(engine: Engine) -> None:
    with engine.begin() as conn:
        sync_entities(conn, [1, 2, 3])
        conn.execute(delete(Entity).where(Entity.id == 3))
        sync_entities(conn, [3])
    assert set(_rows(engine)) == {1, 2}
    assert _search(engine, "blue") == []


def test_sync_is_idempotent(engine: Engine) -> None:
    with engine.begin() as conn:
        sync_entities(conn, [1, 2, 3])
        sync_entities(conn, [1, 1, 2, 3])
    assert len(_rows(engine)) == 3


def test_text_fields_from_a_theme(engine: Engine) -> None:
    def source(conn: Connection, ids: Sequence[int]) -> Mapping[int, Sequence[str | None]]:
        return {2: ["Björk", "Iceland"]}

    with engine.begin() as conn:
        sync_entities(conn, [1, 2, 3], text_source=source)
    assert _rows(engine)[2] == ("Homogenic", "Björk Iceland")
    assert _search(engine, "bjork") == [2]  # diacritics ignored


def test_rebuild_from_scratch_and_drops_orphans(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(insert(entity_fts).values(rowid=99, title="orphan", body=""))
        conn.execute(insert(entity_fts).values(rowid=1, title="stale title", body=""))
        assert rebuild_search_index(conn) == 3
    assert set(_rows(engine)) == {1, 2, 3}
    assert _rows(engine)[1] == ("Abbey Road", "remastered")
    with engine.connect() as conn:
        assert index_is_consistent(conn)


def test_rebuild_in_batches(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(Entity),
            [
                {"id": i, "type": "t", "title": f"Track {i:04}", "extra": {}}
                for i in range(10, 1_260)
            ],
        )
        assert rebuild_search_index(conn) == 1_253
        assert index_is_consistent(conn)
    assert _search(engine, "track 1234") == [1234]
