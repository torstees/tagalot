# Look-alike characters (en dash, fullwidth letters) are the point of these tests.
# ruff: noqa: RUF001
"""Hostile and unusual search-box input (issue #34): FTS5 syntax, punctuation, Unicode, size.

Every input must be treated as plain text and must never raise. Titles here contain FTS5
syntax literally, so a query that leaked into FTS5 syntax would find the wrong rows.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert

from tagalot.core.db import create_keep_engine
from tagalot.core.fts import rebuild_search_index
from tagalot.core.models import Base, Entity
from tagalot.core.search import count_matches, run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tags import TagTree

TITLES = {
    1: "Abbey Road",
    2: 'Mix: title:abbey (live) -remix ^edit NEAR(a b) {x} [y] + NOT "q"',
    3: "“Smart” quotes – en dash",
    4: "Emoji 🎸 Guitar",
    5: r"C:\Music\Beatles",
    6: "AC/DC 50% off_sale",
    7: "Ｆｕｌｌｗｉｄｔｈ Ｔｉｔｌｅ",
}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(Entity), [{"id": i, "type": "t", "title": t} for i, t in TITLES.items()]
        )
        rebuild_search_index(conn)
    yield engine
    engine.dispose()


def _search(engine: Engine, text: str) -> set[int]:
    with engine.connect() as conn:
        return {h.id for h in run_search(conn, SearchSpec(text=text), TagTree([], {}), limit=None)}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # FTS5 syntax is literal text.
        pytest.param("title:abbey", {2}, id="column filter"),
        pytest.param("-remix", {2}, id="leading minus"),
        pytest.param("^edit", {2}, id="caret"),
        pytest.param("NEAR(a", {2}, id="NEAR"),
        pytest.param("(live)", {2}, id="parentheses"),
        pytest.param("{x}", {2}, id="braces"),
        pytest.param("[y]", {2}, id="brackets"),
        pytest.param("+", {2}, id="plus"),
        pytest.param("NOT", {2}, id="NOT"),
        pytest.param('"q"', {2}, id="quoted term"),
        pytest.param('"', {2}, id="lone double quote"),
        pytest.param('"""', set(), id="three double quotes"),
        pytest.param("***", set(), id="stars"),
        pytest.param("abbey OR guitar", set(), id="OR is a term, not an operator"),
        pytest.param("abbey road", {1}, id="ordinary terms"),
        # Punctuation and escapes.
        pytest.param(r"C:\Music", {5}, id="backslashes"),
        pytest.param("\\", {5}, id="single backslash"),
        pytest.param("AC/DC", {6}, id="slash"),
        pytest.param("/", {6}, id="lone slash (the LIKE escape character)"),
        pytest.param("50%", {6}, id="percent"),
        pytest.param("%", {6}, id="lone percent"),
        pytest.param("_", {6}, id="lone underscore"),
        pytest.param("_f", set(), id="underscore is not a wildcard"),  # would match "of"
        # Unicode.
        pytest.param("“smart”", {3}, id="curly quotes"),
        pytest.param("–", {3}, id="en dash"),
        pytest.param("🎸", {4}, id="emoji alone"),
        pytest.param("guitar 🎸", {4}, id="emoji with a word"),
        pytest.param("Ｆｕｌｌ", {7}, id="fullwidth"),
        pytest.param("\u00a0road\t", {1}, id="non-breaking space and tab"),
    ],
)
def test_input_is_literal_text(engine: Engine, text: str, expected: set[int]) -> None:
    assert _search(engine, text) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a" * 5_000, id="very long term"),
        pytest.param(" ".join(f"t{i}" for i in range(300)), id="300 terms"),
        pytest.param("abc\x00def", id="NUL character"),
        pytest.param("\x1b[31mred\x1b[0m", id="control characters"),
    ],
)
def test_extreme_input_never_raises(engine: Engine, text: str) -> None:
    assert _search(engine, text) == set()
    with engine.connect() as conn:
        assert count_matches(conn, SearchSpec(text=text), TagTree([], {})) == 0


@pytest.mark.parametrize(
    ("text", "cleaned", "expected"),
    [
        pytest.param("abbey\x00road", "abbey road", {1}, id="NUL separates terms"),
        pytest.param("\x1b[31mabbey", "[31mabbey", set(), id="terminal escape"),
        pytest.param("road\ud800", "road", {1}, id="lone surrogate dropped"),
        pytest.param("\x00\ud800\x07", None, None, id="nothing left: no text filter"),
    ],
)
def test_control_characters_and_surrogates_are_cleaned(
    engine: Engine, text: str, cleaned: str | None, expected: set[int] | None
) -> None:
    spec = SearchSpec(text=text)
    assert spec.text == cleaned
    if expected is not None:
        assert _search(engine, text) == expected
    assert SearchSpec.loads(spec.dumps()) == spec  # the cleaned text round-trips
