"""Searching inside documents at scale (#350, DESIGN.md §8): 1,000 documents of 50 pages
each (50,000 pages, about 25 MB of text). Building each index, and a search's first page and
count with Contents on, must stay within generous bounds (the search target is §8's 100 ms;
the bound is five times that, as in ``test_search_benchmark``)."""

import random
import statistics
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection

from tagalot.core.contents import FileText, build_index, document_files, save_texts
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.search import count_matches, run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tests.core.test_contents import THEME

pytestmark = pytest.mark.slow

DOCUMENTS, PAGES, WORDS_PER_PAGE = 1000, 50, 80
BOUND_MS = 500
RUNS = 5
VOCABULARY = [f"word{n}" for n in range(5000)] + ["photosynthesis", "chlorophyll"]


def _median_ms(fn: Callable[[], object]) -> float:
    samples = []
    for _ in range(RUNS):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[KeepSession]:
    base = tmp_path_factory.mktemp("contents-bench")
    themes, files = base / "themes", base / "files"
    themes.mkdir()
    files.mkdir()
    (themes / "docs.py").write_text(THEME, encoding="utf-8")
    for n in range(DOCUMENTS):
        (files / f"doc{n:04}.txt").write_text("x", encoding="utf-8")
    keep = create_keep(
        base / "Bench.keep", "Bench", ThemeRef("docs", 1), [RootConfig("f", "F", str(files))]
    )
    with KeepSession.open(Path(keep.dir), Settings(theme_dirs=[themes])) as opened:
        opened.scan_all()
        opened.set_contents_index("words")
        with opened.reader.connect() as conn:
            docs = document_files(conn, opened.schema)
        assert len(docs) == DOCUMENTS
        rng = random.Random(7)
        texts = [
            FileText(
                doc,
                [" ".join(rng.choices(VOCABULARY[:5000], k=WORDS_PER_PAGE)) for _ in range(PAGES)],
            )
            for doc in docs
        ]
        # A few pages mention the rare words.
        for doc in texts[:20]:
            pages = list(doc.pages)
            pages[3] += " photosynthesis chlorophyll"
            texts[texts.index(doc)] = FileText(doc.file, pages)
        for start in range(0, len(texts), 100):
            batch = texts[start : start + 100]

            def save(conn: Connection, batch: list[FileText] = batch) -> None:
                save_texts(conn, batch)

            opened.contents.writer.run(save)
        yield opened


def _search_ms(session: KeepSession, text: str) -> tuple[float, float, int]:
    spec = SearchSpec(text=text, contents=True)
    tree = session.tag_cache.get()
    with session.reader.connect() as conn:
        scope = session.contents_scope(conn, spec)
        assert scope is not None
        page_ms = _median_ms(lambda: run_search(conn, spec, tree, limit=100, contents=scope))
        count_ms = _median_ms(lambda: count_matches(conn, spec, tree, contents=scope))
        found = count_matches(conn, spec, tree, contents=scope)
    return page_ms, count_ms, found


@pytest.mark.parametrize("index", ["words", "substrings"])
def test_building_and_searching(session: KeepSession, index: str) -> None:
    session.set_contents_index(index)
    start = time.perf_counter()
    pages = session.contents.writer.run(lambda conn: build_index(conn, index))
    build_seconds = time.perf_counter() - start
    assert pages == DOCUMENTS * PAGES
    assert build_seconds < 120, f"building the {index} index took {build_seconds:.0f} s"
    for text, expected in [("photosynthesis", 20), ("chlorophyll photosynth", 20)]:
        page_ms, count_ms, found = _search_ms(session, text)
        print(f"\n{index} {text!r}: page {page_ms:.0f} ms, count {count_ms:.0f} ms")
        assert found == expected
        assert page_ms < BOUND_MS, f"{index} {text!r}: first page took {page_ms:.0f} ms"
        assert count_ms < BOUND_MS, f"{index} {text!r}: count took {count_ms:.0f} ms"
    # A common word: on most documents' pages.
    page_ms, count_ms, found = _search_ms(session, "word123")
    print(f"\n{index} common word: page {page_ms:.0f} ms, count {count_ms:.0f} ms, {found}")
    assert found > DOCUMENTS // 2
    assert page_ms < BOUND_MS, f"{index} common word: first page took {page_ms:.0f} ms"
    assert count_ms < BOUND_MS, f"{index} common word: count took {count_ms:.0f} ms"
    print(f"\n{index}: built {pages:,} pages in {build_seconds:.1f} s")
