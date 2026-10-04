"""The Triage page (#110), on the music demo."""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.keep import load_keep_config
from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.ui.main_window import MainWindow
from tagalot.ui.models.results import KEYWORDS
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tagalot.ui.triage import KEYWORDS_TAB, MISSING_TAB, UNTAGGED_TAB, TriagePage
from tests.ui.test_music_views import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

TRIAGE = NavTarget("triage", label="Triage")


def _triage(qtbot: QtBot, window: MainWindow) -> TriagePage:
    window.navigation.select(TRIAGE)
    page = window.stack.currentWidget()
    assert isinstance(page, TriagePage)
    qtbot.waitUntil(lambda: page.counts is not None, timeout=5000)
    return page


def _tab_texts(page: TriagePage) -> list[str]:
    return [page.tabs.tabText(i) for i in range(page.tabs.count())]


def _select_title(qtbot: QtBot, search: SearchPage, title: str) -> int:
    qtbot.waitUntil(lambda: search.model.rowCount() > 0, timeout=5000)
    qtbot.waitUntil(lambda: search.model.hit(search.model.rowCount() - 1) is not None, timeout=5000)
    for row in range(search.model.rowCount()):
        hit = search.model.hit(row)
        if hit is not None and hit.title == title:
            search.table.selectionModel().select(
                search.model.index(row, 0),
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
            return hit.id
    raise AssertionError(title)


def test_the_three_lists(qtbot: QtBot, window: MainWindow) -> None:
    page = _triage(qtbot, window)
    assert _tab_texts(page) == [
        "Unlinked files (1)",
        "Untagged items (18)",
        "Missing files (0)",
        "Unmatched keywords (10)",  # songs whose genres match no tag (#295)
    ]
    assert [f.relpath for f in page.files.files] == ["Miles Davis/Kind of Blue/cover.jpg"]
    assert window.current_items_page() is None  # the files tab has no items to tag
    page.tabs.setCurrentIndex(UNTAGGED_TAB)
    assert window.current_items_page() is page.untagged


def test_dismissing_a_file_is_undoable(qtbot: QtBot, window: MainWindow) -> None:
    page = _triage(qtbot, window)
    page.table.selectRow(0)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        page._dismiss_files()
    qtbot.waitUntil(lambda: _tab_texts(page)[0] == "Unlinked files (0)", timeout=5000)
    assert window.undo_action.text() == "Undo Dismiss 1 file"
    window.undo_action.trigger()
    qtbot.waitUntil(lambda: _tab_texts(page)[0] == "Unlinked files (1)", timeout=5000)


def test_skipping_a_file_in_scans(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _triage(qtbot, window)
    page.table.selectRow(0)
    page._skip()
    qtbot.waitUntil(
        lambda: "left out from the next scan" in window.statusBar().currentMessage(),
        timeout=5000,
    )
    assert load_keep_config(session.keep.toml_path).roots[0].exclude[-1] == (
        "Miles Davis/Kind of Blue/cover.jpg"
    )
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    qtbot.waitUntil(lambda: _tab_texts(page)[0] == "Unlinked files (0)", timeout=5000)


def test_tagging_untagged_items_and_dismissing_them(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _triage(qtbot, window)
    page.tabs.setCurrentIndex(UNTAGGED_TAB)
    _select_title(qtbot, page.untagged, "Freddie Freeloader")
    tree = session.tag_cache.get()
    calm = tree.find_child(tree.find_child(None, "Mood"), "Calm")
    assert calm is not None
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.tag_selection([calm])
    qtbot.waitUntil(lambda: _tab_texts(page)[1] == "Untagged items (17)", timeout=5000)

    _select_title(qtbot, page.untagged, "Intro")
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        page._dismiss_items()
    qtbot.waitUntil(lambda: _tab_texts(page)[1] == "Untagged items (16)", timeout=5000)


def test_items_whose_files_are_missing(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    nothing_is_launched: list[tuple[str, str]],
    tmp_path: Path,
) -> None:
    root = Path(session.root_path("music"))
    song = root / "Jazz Hits" / "01 Take Five.mp3"
    os.replace(song, tmp_path / song.name)  # gone from the root
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    page = _triage(qtbot, window)
    qtbot.waitUntil(lambda: _tab_texts(page)[2] == "Missing files (1)", timeout=5000)
    page.tabs.setCurrentIndex(MISSING_TAB)
    take_five = _select_title(qtbot, page.missing, "Take Five")

    page._show_where()
    qtbot.waitUntil(lambda: len(nothing_is_launched) == 1, timeout=5000)
    assert Path(nothing_is_launched[0][1]) == root / "Jazz Hits"  # its folder, still there

    asked: list[int] = []
    window.confirm_delete_items = lambda count: not asked.append(count)  # type: ignore[method-assign, func-returns-value]
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        page._delete()
    assert asked == [1]
    qtbot.waitUntil(lambda: _tab_texts(page)[2] == "Missing files (0)", timeout=5000)
    with session.reader.connect() as conn:
        assert conn.scalar(select(Entity.id).where(Entity.id == take_five)) is None
    window.undo_action.trigger()
    qtbot.waitUntil(lambda: _tab_texts(page)[2] == "Missing files (1)", timeout=5000)


def test_unmatched_keywords_show_their_keywords(qtbot: QtBot, window: MainWindow) -> None:
    """The Keywords column (#295) is shown in this list, unmatched keywords first."""
    page = _triage(qtbot, window)
    page.tabs.setCurrentIndex(KEYWORDS_TAB)
    listed = page.keywords
    qtbot.waitUntil(lambda: listed.model.rowCount() == 10, timeout=5000)
    qtbot.waitUntil(lambda: listed.model.hit(9) is not None, timeout=5000)
    keys = [c.key for c in listed.model.columns]
    assert KEYWORDS in keys
    column = keys.index(KEYWORDS)
    assert not listed.table.isColumnHidden(column)
    row = next(r for r in range(10) if (h := listed.model.hit(r)) and h.title == "So What")
    index = listed.model.index(row, column)
    qtbot.waitUntil(lambda: listed.model.data(index) == "Jazz", timeout=5000)
    assert listed.model.data(index, Qt.ItemDataRole.ToolTipRole) == "Jazz: no tag"
