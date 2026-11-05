"""Kind chips on Triage's lists of files (#640), on the music demo."""

from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from tagalot.core.broken_links import Broken
from tagalot.core.session import KeepSession
from tagalot.ui.main_window import MainWindow
from tagalot.ui.triage import ALL_HIDDEN, LINKS_TAB, UNLINKED_TAB, Counts, TriagePage
from tests.core.media_files import write_image
from tests.ui.test_music_views import session, window
from tests.ui.test_triage import _triage

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

BLUE = "Miles Davis/Kind of Blue"


def _chip_texts(page: TriagePage, key: str) -> list[str]:
    return [c.text() for c in page.kind_chips[key].chips.values()]


def _unlinked(page: TriagePage) -> list[str]:
    return [f.relpath for f in page.files.files]


def test_unlinked_files_hide_by_type(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    write_image(Path(session.root_path(session.keep.config.roots[0].id)) / BLUE / "back.png")
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    page = _triage(qtbot, window)
    chips = page.kind_chips["unlinked"]
    qtbot.waitUntil(lambda: len(_unlinked(page)) == 2, timeout=5000)
    assert _chip_texts(page, "unlinked") == ["JPG 1", "PNG 1"]
    assert chips.isVisibleTo(page)

    chips.toggle(".png")
    qtbot.waitUntil(lambda: _unlinked(page) == [f"{BLUE}/cover.jpg"], timeout=5000)
    assert page.tabs.tabText(UNLINKED_TAB) == "Unlinked files (1 of 2)"
    assert _chip_texts(page, "unlinked") == ["JPG 1", "PNG 1"]  # counted as if shown
    assert window._ui_state["hidden_kinds"]["triage:unlinked"] == [".png"]

    chips.toggle(".jpg")
    qtbot.waitUntil(lambda: _unlinked(page) == [], timeout=5000)
    assert page.files_stack.currentWidget() is page.files_empty
    assert page.files_empty.title.text() == ALL_HIDDEN[0]

    chips.toggle(".jpg", only=True)  # Ctrl+click: only JPG
    qtbot.waitUntil(lambda: _unlinked(page) == [f"{BLUE}/cover.jpg"], timeout=5000)
    assert page.files_stack.currentWidget() is page.table
    chips.toggle(".jpg", only=True)  # again: all of them
    qtbot.waitUntil(lambda: len(_unlinked(page)) == 2, timeout=5000)
    assert page.tabs.tabText(UNLINKED_TAB) == "Unlinked files (2)"
    assert "triage:unlinked" not in window._ui_state["hidden_kinds"]


def test_a_list_loaded_whole_is_filtered_on_screen(qtbot: QtBot, window: MainWindow) -> None:
    window._ui_state.setdefault("hidden_kinds", {})["triage:links"] = [".lnk"]
    page = _triage(qtbot, window)
    assert page.kind_chips["links"].hidden == (".lnk",)  # remembered
    links = [
        Broken("r", "Music", "Old/Kind of Blue.lnk", "shortcut", "D:/Music/Kind of Blue"),
        Broken("r", "Music", "Old/So What.mp3", "symlink", "/mnt/music/So What.mp3"),
    ]
    page._show(Counts(0, 0, 0, links=2), [], links=list(links))
    assert [r.relpath for r in page.links.rows] == ["Old/So What.mp3"]
    assert page.tabs.tabText(LINKS_TAB) == "Links (1 of 2)"
    assert _chip_texts(page, "links") == ["LNK 1", "MP3 1"]

    page.kind_chips["links"].toggle(".mp3")
    assert page.links.rows == []
    assert page.links_stack.currentWidget() is page.links_empty
    assert page.links_empty.title.text() == ALL_HIDDEN[0]
    assert window._ui_state["hidden_kinds"]["triage:links"] == [".lnk", ".mp3"]

    page.kind_chips["links"].toggle(".lnk")
    assert [r.relpath for r in page.links.rows] == ["Old/Kind of Blue.lnk"]
    page._show(Counts(0, 0, 0), [])  # nothing to list: the usual empty state
    assert page.links_empty.title.text() == "Every link works"
    assert not page.kind_chips["links"].isVisibleTo(page)  # a hidden kind, but none listed
