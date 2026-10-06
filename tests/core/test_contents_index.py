"""The contents index (#349): Words or Substrings, built from the stored text (switching
reads no file again), kept in step as text changes, dropped by Off (the text stays), and
cleared with the text."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, select

from tagalot.core.contents import (
    built_indexes,
    contents_page,
    index_table,
    ready_index,
)
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tests.core.book_files import paged_pdf
from tests.core.test_contents import THEME, _read


@pytest.fixture
def files(tmp_path: Path) -> Path:
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "botany.pdf").write_bytes(
        paged_pdf([["Leaves and roots"], ["Photosynthesis in the light"]])
    )
    (folder / "notes.txt").write_text("synthetic notes from the café", encoding="utf-8")
    return folder


@pytest.fixture
def session(tmp_path: Path, files: Path) -> Iterator[KeepSession]:
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "docs.py").write_text(THEME, encoding="utf-8")
    keep = create_keep(
        tmp_path / "Docs.keep", "Docs", ThemeRef("docs", 1), [RootConfig("f", "F", str(files))]
    )
    with KeepSession.open(keep.dir, Settings(theme_dirs=[themes])) as opened:
        opened.scan_all()
        yield opened


def _found(session: KeepSession, query: str) -> list[tuple[int, str]]:
    """``(page, text)`` of the pages the ready index matches ``query`` on."""
    with session.contents.reader.connect() as conn:
        name = ready_index(conn)
        assert name is not None
        table = index_table(name)
        rows = conn.execute(
            select(contents_page.c.page, contents_page.c.text)
            .join(table, table.c.rowid == contents_page.c.id)
            .where(table.c[table.name].match(query))
            .order_by(contents_page.c.id)
        )
        return [(page, text) for page, text in rows]


def _check(session: KeepSession) -> None:
    """FTS5's own check that every index agrees with the text."""

    def check(conn: Connection) -> None:
        for name in built_indexes(conn):
            table = index_table(name)
            conn.execute(table.insert().values({table.name: "integrity-check"}))

    session.contents.writer.run(check)


def _turn_on(session: KeepSession, index: str) -> None:
    session.set_contents_index(index)
    session.ensure_contents_index()
    _read(session)


def test_words_finds_whole_words_and_word_starts(session: KeepSession) -> None:
    _turn_on(session, "words")
    assert _found(session, "photosynth*") == [(2, "Photosynthesis in the light")]
    assert _found(session, "cafe") == [(1, "synthetic notes from the café")]  # no accents
    assert _found(session, "synth*") == [(1, "synthetic notes from the café")]  # word starts
    _check(session)


def test_switching_rebuilds_from_the_stored_text(session: KeepSession) -> None:
    _turn_on(session, "words")
    session.set_contents_index("substrings")
    assert session.ensure_contents_index() is True
    assert session.queue_contents() == 0  # nothing is read again
    assert sorted(t for _, t in _found(session, '"synth"')) == [
        "Photosynthesis in the light",
        "synthetic notes from the café",
    ]
    with session.contents.reader.connect() as conn:
        assert built_indexes(conn) == ["substrings"]  # the old one is gone
    assert session.ensure_contents_index() is False  # already the one asked for
    _check(session)


def test_the_index_follows_changed_and_deleted_text(session: KeepSession, files: Path) -> None:
    _turn_on(session, "words")
    (files / "notes.txt").write_text("chlorophyll notes", encoding="utf-8")
    session.scan_all()
    _read(session)
    assert _found(session, "synth*") == []
    assert _found(session, "chlorophyll") == [(1, "chlorophyll notes")]
    _check(session)
    session.delete_root("f")
    _read(session)  # drops the deleted files' text, and their pages from the index
    assert _found(session, "chlorophyll") == []
    _check(session)


def test_off_keeps_the_text_and_clear_deletes_it(session: KeepSession) -> None:
    _turn_on(session, "words")
    session.set_contents_index(None)
    with session.contents.reader.connect() as conn:
        assert built_indexes(conn) == []
        assert ready_index(conn) is None
    stats = session.contents_stats()
    assert stats is not None
    assert (stats.files, stats.pages, stats.index) == (2, 3, None)
    session.set_contents_index("words")
    assert session.ensure_contents_index() is True
    assert session.queue_contents() == 0  # on again: the text was kept
    assert _found(session, "roots") == [(1, "Leaves and roots")]
    session.clear_contents()
    assert session.keep.config.contents_index is None
    stats = session.contents_stats()
    assert stats is not None
    assert (stats.files, stats.pages, stats.index) == (0, 0, None)
