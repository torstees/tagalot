"""Keep configuration fits on the screen (#593): its tabs scroll, so it opens no taller
than the screen, its top and buttons on it."""

import pytest
from PySide6.QtWidgets import QScrollArea
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.ui.keep_config import OPEN_SIZE, KeepConfigWindow
from tests.ui.test_field_columns import session

pytestmark = pytest.mark.gui

__all__ = ["session"]  # the fixture


def test_the_window_opens_on_the_screen(qtbot: QtBot, session: KeepSession) -> None:
    window = KeepConfigWindow(session)
    qtbot.addWidget(window)
    assert all(isinstance(window.tabs.widget(i), QScrollArea) for i in range(window.tabs.count()))
    assert window.minimumSizeHint().height() < 300  # no longer as tall as its tallest tab
    window.show()
    qtbot.waitExposed(window)
    free = window.screen().availableGeometry()
    frame = window.frameGeometry()
    assert frame.top() >= free.top()
    assert frame.left() >= free.left()
    assert window.height() <= max(OPEN_SIZE.height(), free.height() * 9 // 10)
    assert window.height() <= free.height()
