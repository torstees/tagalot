"""Theme cards on the dashboard (#114), on the assets demo."""

import pytest
from PySide6.QtWidgets import QFrame, QLabel
from pytestqt.qtbot import QtBot

from tagalot.core.search_spec import ChoiceFilter
from tagalot.ui.dashboard import DashboardPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tests.ui.test_contents_search import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


def _card_text(page: DashboardPage, title: str) -> str:
    frame = page.findChild(QFrame, f"card_{title.lower().replace(' ', '_')}")
    assert isinstance(frame, QFrame), title
    return " ".join(label.text() for label in frame.findChildren(QLabel))


def test_the_themes_cards_and_their_links(qtbot: QtBot, window: MainWindow) -> None:
    window.navigation.select(NavTarget("dashboard", label="Dashboard"))
    page = window.stack.currentWidget()
    assert isinstance(page, DashboardPage)
    qtbot.waitUntil(lambda: page.data is not None, timeout=5000)
    assert page.theme_grid.count() == 3
    assert "KB" in _card_text(page, "Space used by images") or "MB" in _card_text(
        page, "Space used by images"
    )
    biggest = _card_text(page, "Biggest artists")
    assert "Aurora Studio: 7 assets" in biggest
    assert "Kenji Sato: 4 assets" in biggest
    types = _card_text(page, "Image types")
    assert ".png</a>: 6" in types  # each value links to a search

    jpg = ChoiceFilter("extension", (".jpg",))
    index = next(i for i, (_, f) in enumerate(page._values) if f == jpg)
    page._clicked(f"value:{index}")
    search = window.stack.currentWidget()
    assert isinstance(search, SearchPage)
    assert search.filter_bar.filters().fields == (ChoiceFilter("extension", (".jpg",)),)
    qtbot.waitUntil(lambda: search.status.text() == "3 items", timeout=5000)
