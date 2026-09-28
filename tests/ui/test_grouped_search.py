"""Tests for the global search grouped by type (Search all)."""

import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt, QThreadPool
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot
from sqlalchemy import Connection

from tagalot.core.ingest import IngestSession
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.themes.loader import load_themes
from tagalot.ui.filter_bar import ScopeChip
from tagalot.ui.grouped_results import PREVIEW_ROWS, load_groups
from tagalot.ui.models.results import TagsValue
from tagalot.ui.search_view import SearchPage

pytestmark = pytest.mark.gui

MEDIA = """
from tagalot.themes.api import Entity, Theme, field

class Artist(Entity):
    title_label = "Name"
    country: str | None = field("Country", card=True)

class Album(Entity):
    year: int | None = field("Year", card=True)

class Song(Entity):
    length: int | None = field("Length", card=True)

class Media(Theme):
    id, name, version = "media", "Media", 1
    entities = [Artist, Album, Song]
"""

ARTIST, ALBUM, SONG = "media.artist", "media.album", "media.song"
ALBUMS = [f"Love {i:02}" for i in range(1, 11)]  # more than a section shows


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "media.py").write_text(textwrap.dedent(MEDIA), encoding="utf-8")
    catalog = load_themes(user_dir=tmp_path / "no-user-themes", extra_dirs=[themes])
    keep = create_keep(tmp_path / "Media.keep", "Media", ThemeRef("media", 1))
    with KeepSession.open(keep.dir, Settings(), catalog=catalog) as session:
        artist, album, song = session.theme.entities

        def fill(conn: Connection) -> None:
            ctx = IngestSession(conn, session.schema)
            for name, country in [("Courtney Love", "US"), ("Love", "US"), ("Blur", "UK")]:
                ctx.upsert(artist, name, title=name, country=country)
            for i, title in enumerate([*ALBUMS, "Parklife"]):
                ctx.upsert(album, title, title=title, year=1990 + i)
            for title, length in [("All You Need Is Love", 228), ("Song 2", 122)]:
                ctx.upsert(song, title, title=title, length=length)
            ctx.flush()

        session.writer.run(fill)
        yield session


@pytest.fixture
def page(qtbot: QtBot, session: KeepSession) -> SearchPage:
    page = SearchPage(session, "Search all", SearchSpec(), grouped=True, pool=QThreadPool())
    qtbot.addWidget(page)
    page.resize(900, 700)
    page.show()
    qtbot.waitUntil(lambda: page.status.text() == "16 items", timeout=5000)
    return page


def _search_text(qtbot: QtBot, page: SearchPage, text: str) -> None:
    edit = page.filter_bar.text_edit
    edit.setText(text)
    QTest.keyClick(edit, Qt.Key.Key_Return)


def test_load_groups(session: KeepSession) -> None:
    groups = load_groups(session, SearchSpec(text="love"))
    assert [(g.label, g.count) for g in groups] == [("Artists", 2), ("Albums", 10), ("Songs", 1)]
    artists, albums, _ = groups
    assert [h.title for h, _ in artists.rows] == ["Courtney Love", "Love"]
    assert [c.label for c in artists.columns] == ["Name", "Tags", "Country"]
    assert [c.label for c in albums.columns] == ["Title", "Tags", "Year"]
    assert len(albums.rows) == PREVIEW_ROWS
    assert albums.rows[0][1] == {"year": 1990, "tags": TagsValue("", "")}  # untagged
    assert load_groups(session, SearchSpec(text="zebra")) == []


def test_sections_per_type(page: SearchPage) -> None:
    assert page.showing_groups()
    sections = page.groups.sections
    assert [s.header.text() for s in sections] == ["▾ Artists (3)", "▾ Albums (11)", "▾ Songs (2)"]
    artists, albums, songs = sections
    assert artists.model.headerData(2, Qt.Orientation.Horizontal) == "Country"  # after Tags
    assert artists.model.index(0, 2).data() == "UK"  # Blur, first by name
    assert albums.model.rowCount() == PREVIEW_ROWS
    assert albums.show_all_button.isVisible()
    assert albums.show_all_button.text() == "Show all 11 →"
    assert not songs.show_all_button.isVisible()  # both songs are already shown


def test_show_all_narrows_to_one_type_and_the_chip_undoes_it(
    qtbot: QtBot, page: SearchPage
) -> None:
    albums = page.groups.sections[1]
    with qtbot.waitSignal(page.model.counted, timeout=5000):
        QTest.mouseClick(albums.show_all_button, Qt.MouseButton.LeftButton)
    assert not page.showing_groups()
    assert page.status.text() == "11 items"
    assert [c.label for c in page.model.columns] == ["Title", "Tags", "Year"]
    [chip] = page.filter_bar.findChildren(ScopeChip)
    assert chip.label.text() == "Only: Albums"
    assert page.filter_bar.filters().only == ALBUM

    # Sorting the full list works, and filters keep the narrowing.
    page.table.horizontalHeader().setSortIndicator(2, Qt.SortOrder.DescendingOrder)  # Year
    qtbot.waitUntil(lambda: page.model.index(0, 0).data() == "Parklife", timeout=5000)
    with qtbot.waitSignal(page.model.counted, timeout=5000):
        _search_text(qtbot, page, "love 0")
    assert page.status.text() == "10 items"  # Love 01…10

    # Every type again: "love 0" still only matches albums, so their full list stays.
    with qtbot.waitSignal(page.model.counted, timeout=5000):
        QTest.mouseClick(chip.close_button, Qt.MouseButton.LeftButton)
    assert page.filter_bar.filters().only is None
    assert not page.showing_groups()
    assert page.status.text() == "10 items"  # Love 01…10
    _search_text(qtbot, page, "")
    qtbot.waitUntil(page.showing_groups, timeout=5000)
    assert page.status.text() == "16 items"


def test_one_matching_type_shows_its_full_list(qtbot: QtBot, page: SearchPage) -> None:
    with qtbot.waitSignal(page.model.counted, timeout=5000):
        _search_text(qtbot, page, "song")
    assert not page.showing_groups()
    assert [page.model.index(r, 0).data() for r in range(page.model.rowCount())] == ["Song 2"]
    assert [c.label for c in page.model.columns] == ["Title", "Tags", "Length"]
    assert page.filter_bar.findChildren(ScopeChip) == []  # not narrowed by the user
    _search_text(qtbot, page, "")
    qtbot.waitUntil(page.showing_groups, timeout=5000)
    assert page.status.text() == "16 items"


def test_a_sort_is_kept_where_its_field_exists(qtbot: QtBot, page: SearchPage) -> None:
    _search_text(qtbot, page, "love")  # every type
    qtbot.waitUntil(lambda: page.status.text() == "13 items", timeout=5000)
    with qtbot.waitSignal(page.model.counted, timeout=5000):
        page.show_all(ALBUM)
    page.table.horizontalHeader().setSortIndicator(2, Qt.SortOrder.DescendingOrder)  # Year
    qtbot.waitUntil(lambda: page.model.index(0, 0).data() == "Love 10", timeout=5000)
    with qtbot.waitSignal(page.model.counted, timeout=5000):
        _search_text(qtbot, page, "love 0")
    assert page.model.index(0, 0).data() == "Love 10"  # still by year, newest first
    page.filter_bar.set_only(None)  # every type: "love 0" only matches albums anyway
    qtbot.waitUntil(lambda: not page.model.searching, timeout=5000)
    assert page.model.index(0, 0).data() == "Love 10"  # the Year sort applies again
    _search_text(qtbot, page, "")  # sections: sorted by title, Year isn't shared
    qtbot.waitUntil(page.showing_groups, timeout=5000)
    assert page.groups.sections[1].model.index(0, 0).data() == "Love 01"


def test_nothing_found(qtbot: QtBot, page: SearchPage) -> None:
    _search_text(qtbot, page, "zebra")
    qtbot.waitUntil(lambda: page.status.text() == "Nothing found", timeout=5000)
    assert page.groups.sections == []


def test_folded_sections_stay_folded(qtbot: QtBot, page: SearchPage) -> None:
    page.groups.sections[0].header.click()
    assert not page.groups.sections[0].body.isVisible()
    assert page.groups.sections[0].header.text() == "▸ Artists (3)"
    _search_text(qtbot, page, "o")  # new results: every type still matches
    qtbot.waitUntil(lambda: page.status.text() != "Searching…", timeout=5000)
    assert [s.expanded for s in page.groups.sections] == [False, True, True]


def test_clear_all_removes_the_narrowing(qtbot: QtBot, page: SearchPage) -> None:
    page.show_all(SONG)
    qtbot.waitUntil(lambda: page.status.text() == "2 items", timeout=5000)
    page.filter_bar.clear()
    qtbot.waitUntil(page.showing_groups, timeout=5000)
    assert page.filter_bar.filters().only is None
    assert page.status.text() == "16 items"


def test_a_view_is_never_grouped(qtbot: QtBot, session: KeepSession) -> None:
    spec = SearchSpec(types=(ARTIST, SONG))
    page = SearchPage(session, "People and songs", spec, pool=QThreadPool())
    qtbot.addWidget(page)
    qtbot.waitUntil(lambda: page.status.text() == "5 items", timeout=5000)
    assert not page.showing_groups()
    # Artists call their title "Name", songs "Title"; they share no card fields.
    assert [c.label for c in page.model.columns] == ["Title", "Type", "Tags"]


def test_the_selection_spans_every_section(page: SearchPage) -> None:
    artists, albums, _ = page.groups.sections
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    artists.table.selectionModel().select(artists.model.index(0, 0), flags)
    albums.table.selectionModel().select(albums.model.index(1, 0), flags)
    got: list[list[int]] = []
    page.selected_entity_ids(got.append)
    hits = [artists.model.hit(0), albums.model.hit(1)]
    assert got == [[h.id for h in hits if h is not None]]


def test_refreshed_sections_keep_their_selection(qtbot: QtBot, page: SearchPage) -> None:
    albums = page.groups.sections[1]
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    albums.table.selectionModel().select(albums.model.index(2, 0), flags)
    chosen = albums.model.hit(2)
    page.refresh()
    qtbot.waitUntil(lambda: page.groups.sections[1] is not albums, timeout=5000)
    rebuilt = page.groups.sections[1]
    rows = [i.row() for i in rebuilt.table.selectionModel().selectedRows()]
    assert [rebuilt.model.hit(r) for r in rows] == [chosen]
