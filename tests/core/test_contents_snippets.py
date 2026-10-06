"""Where a document matched (#351): the first matching page of an item's files, with a
snippet around the match (FTS5's, or an excerpt for short substring terms)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity
from tagalot.core.search import MATCH_END, MATCH_START, Snippet, contents_snippets
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tests.core.book_files import paged_pdf
from tests.core.test_contents import THEME, _read


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    themes, files = tmp_path / "themes", tmp_path / "files"
    themes.mkdir()
    files.mkdir()
    (themes / "docs.py").write_text(THEME, encoding="utf-8")
    (files / "botany.pdf").write_bytes(
        paged_pdf(
            [
                ["Leaves and roots"],
                ["In green plants photosynthesis needs light and water"],
                ["More photosynthesis later"],
            ]
        )
    )
    (files / "kitchen.txt").write_text("sugar in the ox cart cafe", encoding="utf-8")
    keep = create_keep(
        tmp_path / "Docs.keep", "Docs", ThemeRef("docs", 1), [RootConfig("f", "F", str(files))]
    )
    with KeepSession.open(keep.dir, Settings(theme_dirs=[themes])) as opened:
        opened.scan_all()
        yield opened


def _snippets(session: KeepSession, index: str, text: str) -> dict[str, Snippet]:
    session.set_contents_index(index)
    session.ensure_contents_index()
    _read(session)
    spec = SearchSpec(text=text, contents=True)
    with session.reader.connect() as conn:
        scope = session.contents_scope(conn, spec)
        assert scope is not None
        titles = dict(conn.execute(select(Entity.id, Entity.title)).all())
        found = contents_snippets(conn, text, scope, list(titles))
    return {titles[i]: s for i, s in found.items()}


def _marked(text: str) -> str:
    return text.replace(MATCH_START, "[").replace(MATCH_END, "]")


def test_the_first_matching_page_with_the_words_marked(session: KeepSession) -> None:
    found = _snippets(session, "words", "photosynth")
    assert list(found) == ["botany.pdf"]
    snippet = found["botany.pdf"]
    assert (snippet.relpath, snippet.page, snippet.pages) == ("botany.pdf", 2, 3)
    assert _marked(snippet.text) == "In green plants [photosynthesis] needs light and water"


def test_long_pages_are_cut_around_the_match(session: KeepSession, tmp_path: Path) -> None:
    words = " ".join(f"w{n}" for n in range(60))
    (tmp_path / "files" / "long.txt").write_text(f"{words} target {words}", encoding="utf-8")
    session.scan_all()
    snippet = _snippets(session, "words", "target")["long.txt"]
    assert "[target]" in _marked(snippet.text)
    assert snippet.text.startswith("…")
    assert snippet.text.endswith("…")
    assert len(snippet.text.split()) <= 14


def test_substrings_and_short_terms(session: KeepSession) -> None:
    found = _snippets(session, "substrings", "ynth")
    assert _marked(found["botany.pdf"].text).count("[ynth]") == 1
    assert "needs light and water" in found["botany.pdf"].text  # words around it, too
    short = _snippets(session, "substrings", "ox")["kitchen.txt"]
    assert _marked(short.text) == "sugar in the [ox] cart cafe"  # an excerpt, by LIKE
    assert short.pages == 1
