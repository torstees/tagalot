"""Each keep remembers its window's size (#594): saved in its ui_state.json on closing, and
used when it opens again, within the screen."""

import pytest
from PySide6.QtCore import QSize, Qt, QThreadPool
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.ui_state import load_ui_state, save_ui_state
from tagalot.ui.main_window import WINDOW_KEY, MainWindow
from tagalot.ui.workers import ScanController
from tests.ui.test_field_columns import session

pytestmark = pytest.mark.gui

__all__ = ["session"]  # the fixture


def _window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    return window


def _save(session: KeepSession, saved: object) -> None:
    state = load_ui_state(session.keep.ui_state_path)
    state[WINDOW_KEY] = saved
    save_ui_state(session.keep.ui_state_path, state)


def test_the_size_is_remembered(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    window.show()
    window.resize(window.minimumSizeHint().width() + 50, 610)
    size = window.size()
    window.close()
    saved = load_ui_state(session.keep.ui_state_path)[WINDOW_KEY]
    assert saved == {"width": size.width(), "height": size.height(), "maximized": False}

    again = _window(qtbot, session)
    free = again.screen().availableGeometry().size()
    assert again.size() == size.boundedTo(free)  # the test screen is 800 wide
    again.close()


def test_a_maximized_window_opens_maximized(qtbot: QtBot, session: KeepSession) -> None:
    _save(session, {"width": 1300, "height": 600, "maximized": True})
    window = _window(qtbot, session)
    assert window.windowState() & Qt.WindowState.WindowMaximized


@pytest.mark.parametrize("saved", [{"width": "wide"}, "big", None], ids=str)
def test_an_unreadable_size_is_the_default(
    qtbot: QtBot, session: KeepSession, saved: object
) -> None:
    default = _window(qtbot, session).size()
    _save(session, saved)
    assert _window(qtbot, session).size() == default


def test_a_size_larger_than_the_screen_fits_it(qtbot: QtBot, session: KeepSession) -> None:
    _save(session, {"width": 99999, "height": 99999, "maximized": False})
    window = _window(qtbot, session)
    free = window.screen().availableGeometry().size()
    assert window.height() <= max(free.height(), window.minimumSizeHint().height())
    assert window.width() <= max(free.width(), window.minimumSizeHint().width())
    assert window.size() != QSize(99999, 99999)
