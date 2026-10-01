"""The dashboard page (#113), on the music demo."""

import pytest
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.ui.dashboard import DashboardPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tagalot.ui.triage import UNTAGGED_TAB, TriagePage
from tests.ui.test_contents_search import _id
from tests.ui.test_music_views import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

DASHBOARD = NavTarget("dashboard", label="Dashboard")


def _dashboard(qtbot: QtBot, window: MainWindow) -> DashboardPage:
    window.navigation.select(DASHBOARD)
    page = window.stack.currentWidget()
    assert isinstance(page, DashboardPage)
    qtbot.waitUntil(lambda: page.data is not None, timeout=5000)
    return page


def test_the_cards(qtbot: QtBot, window: MainWindow) -> None:
    page = _dashboard(qtbot, window)
    assert page.title.text() == "Music"
    assert "Songs</a>: 12" in page.items.text()
    assert "24 in all" in page.items.text()
    assert "18 of 24 items (75%)" in page.untagged.text()
    assert "Music files</a>: online, scanned" in page.folders.text()
    assert "Favorites</a>: 2" in page.most_used.text()
    assert page.recent.count() == 12


def test_the_links_go_where_they_say(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _dashboard(qtbot, window)
    page._clicked("type:music.song")
    songs = window.stack.currentWidget()
    assert isinstance(songs, SearchPage)
    qtbot.waitUntil(lambda: songs.status.text() == "12 items", timeout=5000)

    page._clicked("triage:")
    triage = window.stack.currentWidget()
    assert isinstance(triage, TriagePage)
    assert triage.tabs.currentIndex() == UNTAGGED_TAB

    tree = session.tag_cache.get()
    favorites = tree.find_child(None, "Favorites")
    assert favorites is not None
    page._clicked(f"tag:{favorites}")
    search = window.stack.currentWidget()
    assert isinstance(search, SearchPage)
    assert search.filter_bar.filters().include == (favorites,)

    page._clicked("root:music")
    assert window.keep_config is not None
    assert window.keep_config.current_root().id == "music"  # type: ignore[union-attr]


def test_it_follows_tagging(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _dashboard(qtbot, window)
    tree = session.tag_cache.get()
    calm = tree.find_child(tree.find_child(None, "Mood"), "Calm")
    assert calm is not None
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.tag_actions.apply([_id(session, "Freddie Freeloader")], [calm], "Calm")
    qtbot.waitUntil(lambda: "17 of 24" in page.untagged.text(), timeout=5000)
