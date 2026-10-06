"""Searching inside documents (#350): with ``SearchSpec.contents``, the text also matches
items one of whose documents' pages contains every term, in the keep's index (Words: the
last word as a word start; Substrings: inside words, ``LIKE`` for short terms)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.search import contents_terms, count_by_type, count_matches, run_search
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
        paged_pdf([["Leaves and roots"], ["Photosynthesis needs light"]])
    )
    (files / "kitchen.txt").write_text("synthetic sugar in the cafe", encoding="utf-8")
    (files / "roots.txt").write_text("nothing here about plants", encoding="utf-8")
    (files / "ox.note").write_text("an ox, light and leaves", encoding="utf-8")  # not a document
    keep = create_keep(
        tmp_path / "Docs.keep", "Docs", ThemeRef("docs", 1), [RootConfig("f", "F", str(files))]
    )
    with KeepSession.open(keep.dir, Settings(theme_dirs=[themes])) as opened:
        opened.scan_all()
        yield opened


def _titles(session: KeepSession, spec: SearchSpec) -> list[str]:
    tree = session.tag_cache.get()
    with session.reader.connect() as conn:
        contents = session.contents_scope(conn, spec)
        hits = run_search(conn, spec, tree, contents=contents)
        assert count_matches(conn, spec, tree, contents=contents) == len(hits)
    return sorted(h.title for h in hits)


def _turn_on(session: KeepSession, index: str) -> None:
    session.set_contents_index(index)
    session.ensure_contents_index()
    _read(session)


@pytest.mark.parametrize(
    ("index", "text", "expected"),
    [
        # Words: whole words, the last as a word start; every word on one page.
        ("words", "light", ["botany.pdf"]),
        ("words", "photo", ["botany.pdf"]),
        ("words", "needs photo", ["botany.pdf"]),
        ("words", "synth", ["kitchen.txt"]),  # a word start, not inside "photosynthesis"
        ("words", "leaves light", []),  # on different pages
        ("words", "roots", ["botany.pdf", "roots.txt"]),  # roots.txt by its title too
        ("words", '"sugar', ["kitchen.txt"]),  # a stray quote is just a character
        # Substrings: inside words; short terms too.
        ("substrings", "synth", ["botany.pdf", "kitchen.txt"]),
        ("substrings", "ight", ["botany.pdf"]),
        ("substrings", "ca", ["kitchen.txt"]),
        ("substrings", "sugar ca", ["kitchen.txt"]),
        ("substrings", "leaves light", []),
    ],
)
def test_matching_inside_documents(
    session: KeepSession, index: str, text: str, expected: list[str]
) -> None:
    _turn_on(session, index)
    assert _titles(session, SearchSpec(text=text, contents=True)) == expected


def test_only_with_the_toggle_and_only_documents(session: KeepSession) -> None:
    _turn_on(session, "words")
    assert _titles(session, SearchSpec(text="light")) == []  # titles and fields only
    assert _titles(session, SearchSpec(text="ox", contents=True)) == ["ox.note"]  # its title
    assert _titles(session, SearchSpec(text="leaves", contents=True)) == ["botany.pdf"]


def test_nothing_inside_documents_without_an_index(session: KeepSession) -> None:
    _read(session)  # Off: nothing read, nothing searched
    assert _titles(session, SearchSpec(text="light", contents=True)) == []
    _turn_on(session, "words")
    session.set_contents_index(None)  # Off again: the text stays, the index goes
    assert _titles(session, SearchSpec(text="light", contents=True)) == []


def test_counting_by_type(session: KeepSession) -> None:
    _turn_on(session, "words")
    spec = SearchSpec(text="light", contents=True)
    with session.reader.connect() as conn:
        counts = count_by_type(
            conn, spec, session.tag_cache.get(), contents=session.contents_scope(conn, spec)
        )
    assert counts == {"docs.doc": 1}


def test_query_terms() -> None:
    assert contents_terms("needs  photo", "words") == ('"needs" "photo"*', [])
    assert contents_terms('say "hi"', "words") == ('"say" """hi"""*', [])
    assert contents_terms("sugar ca", "substrings") == ('"sugar"', ["ca"])
    assert contents_terms("ca", "substrings") == (None, ["ca"])


def test_the_toggle_is_saved_with_a_search() -> None:
    spec = SearchSpec(text="light", contents=True)
    assert spec.to_json()["contents"] is True
    assert SearchSpec.from_json(spec.to_json()) == spec
    assert "contents" not in SearchSpec(text="light").to_json()
