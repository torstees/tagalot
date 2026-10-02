"""A keep opens on its dashboard (#115)."""

import pytest
from PySide6.QtCore import QThreadPool
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.ui import main_window
from tagalot.ui.dashboard import DashboardPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.workers import ScanController
from tests.ui.test_music_views import session

pytestmark = pytest.mark.gui

__all__ = ["session"]  # fixture


def test_a_keep_opens_on_its_dashboard(
    qtbot: QtBot, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_window, "OPENING_PAGE", NavTarget("dashboard", label="Dashboard"))
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    page = window.stack.currentWidget()
    assert isinstance(page, DashboardPage)
    assert window.navigation.currentIndex().data() == "Dashboard"  # highlighted
    qtbot.waitUntil(lambda: page.data is not None, timeout=5000)
    assert not window.back_action.isEnabled()  # where you start, not a step to go back to
    window.thumbnails.clear()
    window.thumbnails.wait()


def test_the_opening_page_is_the_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The constant itself (the conftest swaps it for most tests; undo that here)."""
    monkeypatch.undo()
    assert NavTarget("dashboard", label="Dashboard") == main_window.OPENING_PAGE
