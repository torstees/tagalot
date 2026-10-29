"""Benchmark: a field of the user's own on the 50k keep (#583, DESIGN.md §8 performance).

Every song (45,000) and album has a value of the keep's own ``plays`` field (an integer)
in ``entity_field``; a list of them sorted by it, filtered to a range of it, and both, must
return its first page and count within the search benchmark's bound.
"""

import random
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import ColumnElement, Engine, insert

from tagalot.core.db import create_keep_engine
from tagalot.core.fields import own_field_info
from tagalot.core.models import Base, EntityField
from tagalot.core.own_fields import OwnField
from tagalot.core.search import CORE_FIELDS, count_matches, run_search
from tagalot.core.search_fields import own_expression
from tagalot.core.search_spec import RangeFilter, SearchSpec, SortKey
from tagalot.core.tags import TagTree
from tests.core.bench_data import BenchKeep, build
from tests.core.test_search_benchmark import ALBUM, BOUND_MS, SONG, _median_ms

pytestmark = pytest.mark.slow

PLAYS = OwnField(SONG, "plays", "Plays", "integer")


@pytest.fixture(scope="module")
def keep(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Engine, BenchKeep]]:
    engine = create_keep_engine(tmp_path_factory.mktemp("own-bench") / "keep.db")
    Base.metadata.create_all(engine)
    rng = random.Random(11)
    with engine.begin() as conn:
        data = build(conn)
        rows = [
            {"entity_id": i, "field": "plays", "integer": rng.randrange(0, 5000)}
            for i in data.songs + data.albums
        ]
        conn.execute(insert(EntityField), rows)
    yield engine, data
    engine.dispose()


def _fields() -> dict[str, ColumnElement[Any]]:
    return {**CORE_FIELDS, PLAYS.key: own_expression(own_field_info(PLAYS))}


SPECS: dict[str, Callable[[], SearchSpec]] = {
    "sorted by it": lambda: SearchSpec(types=(SONG,), sort=(SortKey(PLAYS.key, True),)),
    "a range of it": lambda: SearchSpec(
        types=(SONG,), fields=(RangeFilter(PLAYS.key, 1000, 1200),)
    ),
    "a range, sorted": lambda: SearchSpec(
        types=(SONG,),
        fields=(RangeFilter(PLAYS.key, 100, None),),
        sort=(SortKey(PLAYS.key),),
    ),
    "two types, sorted": lambda: SearchSpec(types=(SONG, ALBUM), sort=(SortKey(PLAYS.key),)),
}


@pytest.mark.parametrize("make_spec", SPECS.values(), ids=SPECS.keys())
def test_first_page_and_count(
    keep: tuple[Engine, BenchKeep], make_spec: Callable[[], SearchSpec]
) -> None:
    engine, _ = keep
    spec, fields = make_spec(), _fields()
    with engine.connect() as conn:
        tree = TagTree.load(conn)
        assert run_search(conn, spec, tree, limit=100, fields=fields), "no results"
        page_ms = _median_ms(lambda: run_search(conn, spec, tree, limit=100, fields=fields))
        count_ms = _median_ms(lambda: count_matches(conn, spec, tree, fields))
    assert page_ms < BOUND_MS, f"first page took {page_ms:.0f} ms"
    assert count_ms < BOUND_MS, f"count took {count_ms:.0f} ms"
