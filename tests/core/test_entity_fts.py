"""Tests for the entity_fts text-search table (DESIGN.md §5, §8)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, delete, insert, inspect, literal_column, select, update

from tagalot.core.db import create_keep_engine
from tagalot.core.models import FTS_TOKENIZER, Base, entity_fts

ROWS = [
    (1, "Abbey Road", "The Beatles 1969"),
    (2, "Beyoncé — Lemonade", ""),
    (3, "IMG_2031_final.png", ""),
    (4, "Crème Brûlée recipes", "dessert"),
]


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(entity_fts), [{"rowid": r, "title": t, "body": b} for r, t, b in ROWS])
    yield engine
    engine.dispose()


def _match(engine: Engine, query: str) -> set[int]:
    stmt = select(entity_fts.c.rowid).where(literal_column(entity_fts.name).match(query))
    with engine.connect() as conn:
        return set(conn.execute(stmt).scalars())


def test_created_with_the_core_schema(engine: Engine) -> None:
    assert "entity_fts" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        sql: str = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'entity_fts'"
        ).scalar_one()
    assert "fts5" in sql
    assert FTS_TOKENIZER == "trigram remove_diacritics 1"
    assert FTS_TOKENIZER in sql


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ('"bey"', {1, 2}),  # substring inside a word: "Abbey", "Beyoncé"
        ('"BEY"', {1, 2}),  # case-insensitive
        ('"beyonce"', {2}),  # diacritics ignored
        ('"beatles"', {1}),  # body column
        ('"2031"', {3}),  # underscores and digits in file names
        ('"creme brulee"', {4}),  # a quoted phrase with a space
        ('"abbey" "1969"', {1}),  # terms are ANDed, across columns
        ('"road" "lemonade"', set()),
        ('"xyz"', set()),
    ],
)
def test_trigram_matching(engine: Engine, query: str, expected: set[int]) -> None:
    assert _match(engine, query) == expected


def test_terms_under_three_characters_need_the_like_fallback(engine: Engine) -> None:
    # DESIGN.md §8: trigram MATCH cannot use 1-2 character terms; search falls back to LIKE.
    assert _match(engine, '"ab"') == set()
    stmt = select(entity_fts.c.rowid).where(entity_fts.c.title.like("%ab%"))
    with engine.connect() as conn:
        assert set(conn.execute(stmt).scalars()) == {1}


def test_rows_can_be_updated_and_deleted(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(update(entity_fts).where(entity_fts.c.rowid == 1).values(title="Let It Be"))
        conn.execute(delete(entity_fts).where(entity_fts.c.rowid == 2))
    assert _match(engine, '"abbey"') == set()
    assert _match(engine, '"let it"') == {1}
    assert _match(engine, '"beyonce"') == set()


def test_drop_all_removes_it(engine: Engine) -> None:
    Base.metadata.drop_all(engine)
    assert inspect(engine).get_table_names() == []
