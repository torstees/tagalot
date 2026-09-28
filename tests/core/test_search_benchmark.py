"""Search benchmark (DESIGN.md §8 performance target, issue #40).

A 50,500-entity keep; every kind of search must return its first page, and its count,
within a generous bound. The design target is 100 ms on a local SSD; the bound here is five
times that so CI runners don't flake, while still catching the regressions this benchmark
was built to find (the first version took 3.3 s for show_contained and 6.1 s for a combined
search). Measured medians on the development machine are recorded in DESIGN.md §8.
"""

import statistics
import time
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, func, select

from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Entity
from tagalot.core.search import count_by_type, count_matches, run_search
from tagalot.core.search_spec import SearchSpec, SortKey
from tagalot.core.tags import TagTree, tag_usage
from tests.core.bench_data import BenchKeep, build

pytestmark = pytest.mark.slow

TARGET_MS = 100
BOUND_MS = 5 * TARGET_MS
RUNS = 5

ALBUM, SONG = "music.album", "music.song"


@pytest.fixture(scope="module")
def keep(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Engine, BenchKeep, float]]:
    engine = create_keep_engine(tmp_path_factory.mktemp("bench") / "keep.db")
    Base.metadata.create_all(engine)
    start = time.perf_counter()
    with engine.begin() as conn:
        data = build(conn)
    build_seconds = time.perf_counter() - start
    yield engine, data, build_seconds
    engine.dispose()


def _median_ms(fn: Callable[[], object]) -> float:
    fn()  # warm up
    samples = []
    for _ in range(RUNS):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


SPECS: dict[str, Callable[[BenchKeep], SearchSpec]] = {
    "everything by title": lambda k: SearchSpec(),
    "one type": lambda k: SearchSpec(types=(SONG,)),
    "newest first": lambda k: SearchSpec(sort=(SortKey("created_at", descending=True),)),
    "a leaf tag": lambda k: SearchSpec(include=(k.leaf_tags[5],)),
    "a 90-tag subtree": lambda k: SearchSpec(include=(k.top_tags[0],)),
    "two groups": lambda k: SearchSpec(include=(k.top_tags[0], k.top_tags[1])),
    "exclude a subtree": lambda k: SearchSpec(exclude=(k.top_tags[2],)),
    "text": lambda k: SearchSpec(text="river"),
    "short text (LIKE)": lambda k: SearchSpec(text="ri"),
    "inherited include": lambda k: SearchSpec(
        types=(SONG,), include=(k.top_tags[0],), inherit_tags=True
    ),
    "inherited exclude": lambda k: SearchSpec(
        types=(SONG,), exclude=(k.top_tags[0],), inherit_tags=True
    ),
    "within an artist": lambda k: SearchSpec(within=k.artists[10]),
    "aggregate up": lambda k: SearchSpec(
        types=(ALBUM,), include=(k.leaf_tags[3],), aggregate_up=True
    ),
    "show contained": lambda k: SearchSpec(
        types=(ALBUM,), include=(k.top_tags[1],), show_contained=True
    ),
    "everything at once": lambda k: SearchSpec(
        types=(ALBUM,),
        include=(k.top_tags[0],),
        exclude=(k.top_tags[2],),
        text="love",
        inherit_tags=True,
        aggregate_up=True,
        show_contained=True,
    ),
}


def test_keep_size_and_build_time(keep: tuple[Engine, BenchKeep, float]) -> None:
    engine, _, build_seconds = keep
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(Entity)) == 50_500
    # Closure maintenance once took 20 s here (a cycle query per edge); now about 2 s.
    assert build_seconds < 30


@pytest.mark.parametrize("make_spec", SPECS.values(), ids=SPECS.keys())
def test_first_page_and_count(
    keep: tuple[Engine, BenchKeep, float], make_spec: Callable[[BenchKeep], SearchSpec]
) -> None:
    engine, data, _ = keep
    spec = make_spec(data)
    with engine.connect() as conn:
        tree = TagTree.load(conn)
        page_ms = _median_ms(lambda: run_search(conn, spec, tree, limit=100))
        count_ms = _median_ms(lambda: count_matches(conn, spec, tree))
        by_type_ms = _median_ms(lambda: count_by_type(conn, spec, tree))
    assert page_ms < BOUND_MS, f"first page took {page_ms:.0f} ms"
    assert count_ms < BOUND_MS, f"count took {count_ms:.0f} ms"
    assert by_type_ms < BOUND_MS, f"count by type took {by_type_ms:.0f} ms"


def test_tag_usage_counts(keep: tuple[Engine, BenchKeep, float]) -> None:
    """The tag manager's usage columns for all 300 tags (each applied at every level)."""
    engine, _, _ = keep
    with engine.connect() as conn:
        tree = TagTree.load(conn)
        usage_ms = _median_ms(lambda: tag_usage(conn, tree))
    print(f"tag usage for {len(tree)} tags: {usage_ms:.0f} ms")
    assert usage_ms < 2000, f"tag usage took {usage_ms:.0f} ms"  # it runs in a worker
