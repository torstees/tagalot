"""Back and forward through pages, breadcrumbs, and the navigation highlight (#88)."""

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.ui import main_window
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tests.ui.test_contents_search import _id, _open, session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

SEARCH_ALL = NavTarget("search", label="Search all")
ARTISTS = NavTarget("view", key="Artists", label="Artists")


def _where(window: MainWindow) -> str:
    """The current page: a detail page's title, or the navigation target's label."""
    page = window.stack.currentWidget()
    if isinstance(page, DetailPage):
        return page.detail.title if page.detail is not None else "…"
    target = next(t for t, p in window._pages.items() if p is page)
    return target.label


def _visit(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    """Search all → Artists → Aurora Studio → forest.png."""
    window.navigation.select(ARTISTS)
    _open(qtbot, window, session, "Aurora Studio")
    _open(qtbot, window, session, "forest.png")


def test_back_and_forward(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    assert not window.back_action.isEnabled()
    _visit(qtbot, window, session)
    assert _where(window) == "forest.png"
    assert window.back_action.isEnabled()
    assert not window.forward_action.isEnabled()

    steps = []
    for _ in range(3):
        window.back_action.trigger()
        qtbot.waitUntil(lambda: _where(window) != "…")
        steps.append(_where(window))
    assert steps == ["Aurora Studio", "Artists", "Search all"]
    assert not window.back_action.isEnabled()

    window.forward_action.trigger()
    assert _where(window) == "Artists"
    assert window.forward_action.isEnabled()


def test_a_new_page_drops_the_forward_pages(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    _visit(qtbot, window, session)
    window.back()
    window.back()  # at Artists
    _open(qtbot, window, session, "Kenji Sato")
    assert not window.forward_action.isEnabled()
    window.back()
    assert _where(window) == "Artists"
    window.back()
    assert _where(window) == "Search all"


def test_showing_the_same_page_again_isnt_a_step(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    window.navigation.select(ARTISTS)
    window.navigation.select(ARTISTS)  # clicked twice
    window.back()
    assert _where(window) == "Search all"


def test_the_navigation_highlights_the_current_page(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    nav = window.navigation
    _visit(qtbot, window, session)
    assert not nav.currentIndex().isValid()  # a detail page isn't listed
    window.back()
    window.back()
    assert nav.currentIndex() == nav.index_of(ARTISTS)
    window.back()
    assert nav.currentIndex() == nav.index_of(SEARCH_ALL)


def test_the_mouse_back_and_forward_buttons(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    _visit(qtbot, window, session)

    def press(button: Qt.MouseButton) -> None:
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(5, 5),
            QPointF(5, 5),
            button,
            button,
            Qt.KeyboardModifier.NoModifier,
        )
        page = window.stack.currentWidget()
        assert page is not None
        QApplication.sendEvent(page, event)

    press(Qt.MouseButton.BackButton)
    assert _where(window) == "Aurora Studio"
    press(Qt.MouseButton.ForwardButton)
    assert _where(window) == "forest.png"


def test_breadcrumbs_open_the_container(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    forest = _open(qtbot, window, session, "forest.png")
    assert forest.breadcrumbs.isVisible()
    assert "Aurora Studio" in forest.breadcrumbs.text()
    forest.breadcrumbs.linkActivated.emit(str(_id(session, "Aurora Studio")))
    qtbot.waitUntil(lambda: _where(window) == "Aurora Studio", timeout=5000)
    aurora = window.stack.currentWidget()
    assert isinstance(aurora, DetailPage)
    assert not aurora.breadcrumbs.isVisible()  # an artist is at the top
    window.back()
    assert window.stack.currentWidget() is forest


def test_old_detail_pages_are_let_go(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_window, "MAX_DETAIL_PAGES", 2)
    for title in ["gem.png", "heart.png", "forest.png"]:
        _open(qtbot, window, session, title)
    details = [p for p in window._pages.values() if isinstance(p, DetailPage)]
    assert [p.detail.title for p in details if p.detail] == ["heart.png", "forest.png"]
    window.back()
    window.back()  # gem.png's page is made again
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: page.detail is not None, timeout=5000)
    assert page.detail is not None
    assert page.detail.title == "gem.png"
