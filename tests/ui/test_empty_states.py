"""The knight (#360): empty pages say why there's nothing and what to do, and the splash
screen while a keep opens from the command line."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QPushButton
from pytestqt.qtbot import QtBot

from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.resources import TOWER_SIZE, knight_file
from tagalot.ui.dashboard import DashboardPage
from tagalot.ui.empty_state import EmptyState, splash_screen
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tagalot.ui.triage import MISSING_TAB, TriagePage
from tagalot.ui.workers import ScanController
from tests.ui.test_music_views import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


def test_the_knight_is_shipped() -> None:
    with Image.open(str(knight_file())) as image:
        assert image.size == (TOWER_SIZE, TOWER_SIZE)


def _search(qtbot: QtBot, window: MainWindow, target: NavTarget) -> SearchPage:
    window.navigation.select(target)
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    qtbot.waitUntil(lambda: page.status.text() not in ("", "Searching…"), timeout=5000)
    return page


def test_a_search_that_finds_nothing(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, NavTarget("search", label="Search all"))
    assert not page.showing_empty()
    page.filter_bar.set_text("zzzz nothing like this")
    qtbot.waitUntil(page.showing_empty, timeout=5000)
    assert page.empty.title.text() == "Nothing found"
    assert "Try other words" in page.empty.hint.text()
    assert page.empty.knight.isVisibleTo(page.empty)
    page.filter_bar.set_text("")
    qtbot.waitUntil(lambda: not page.showing_empty(), timeout=5000)


def test_switching_layouts_keeps_the_empty_page(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, NavTarget("view", key="Songs", label="Songs"))
    page.filter_bar.set_text("zzzz")
    qtbot.waitUntil(page.showing_empty, timeout=5000)
    page.set_layout("grid")
    assert page.showing_empty()


def test_an_empty_triage_list(qtbot: QtBot, window: MainWindow) -> None:
    window.navigation.select(NavTarget("triage", label="Triage"))
    triage = window.stack.currentWidget()
    assert isinstance(triage, TriagePage)
    triage.tabs.setCurrentIndex(MISSING_TAB)
    qtbot.waitUntil(triage.missing.showing_empty, timeout=5000)  # the demo misses nothing
    assert triage.missing.empty.title.text() == "Nothing to tidy here"
    assert triage.missing.empty.hint.text() == "No item's files are missing."


@pytest.fixture
def empty_session(tmp_path: Path) -> Iterator[KeepSession]:
    keep = create_keep(tmp_path / "Empty.keep", "Empty", ThemeRef("generic", 1))
    with KeepSession.open(keep.dir, Settings()) as opened:
        yield opened


def test_an_empty_keep(qtbot: QtBot, empty_session: KeepSession) -> None:
    window = MainWindow(empty_session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.navigation.select(NavTarget("dashboard", label="Dashboard"))
    dashboard = window.stack.currentWidget()
    assert isinstance(dashboard, DashboardPage)
    qtbot.waitUntil(lambda: dashboard.data is not None, timeout=5000)
    assert dashboard.empty.isVisibleTo(dashboard)
    assert not dashboard.cards.isVisibleTo(dashboard)
    configured: list[bool] = []
    dashboard.configure_requested.connect(lambda: configured.append(True))
    buttons = {b.text(): b for b in dashboard.empty.findChildren(QPushButton)}
    assert set(buttons) == {"Configure keep…", "Scan now"}
    buttons["Configure keep…"].click()
    assert configured == [True]

    files = _search(qtbot, window, NavTarget("view", key="Files", label="Files"))
    qtbot.waitUntil(files.showing_empty, timeout=5000)
    assert files.empty.title.text() == "Nothing here yet"
    assert "Configure keep" in files.empty.hint.text()


def test_compact_and_buttons(qtbot: QtBot) -> None:
    state = EmptyState("Nothing", "Do this")
    qtbot.addWidget(state)
    state.set_compact(True)
    assert not state.knight.isVisibleTo(state)
    clicked: list[int] = []
    button = state.add_button("Go", lambda: clicked.append(1))
    button.click()
    assert clicked == [1]


def test_the_splash(qtbot: QtBot) -> None:
    splash = splash_screen("Music")
    qtbot.addWidget(splash)
    assert splash.message() == "Opening Music…"
    assert not splash.pixmap().isNull()
