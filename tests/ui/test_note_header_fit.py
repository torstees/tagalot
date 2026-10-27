"""The note in a real page's header settles after the window is resized, and the header
fits itself again after Show more (#553's review: the note and its buttons jittered up and
down endlessly, then the app crashed; Show more didn't grow the header)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QObject, QThreadPool
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.note_view import PICTURES_DELAY
from tagalot.ui.workers import ScanController
from tests.ui.test_contents_search import SCRIPT, _open

pytestmark = pytest.mark.gui


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keep = module.make_assets_demo(tmp_path)  # Aurora Studio's note: a picture, a table
    with KeepSession.open(keep, Settings()) as session:
        session.scan_all()
        yield session


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> Iterator[MainWindow]:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()
    yield window
    window.thumbnails.clear()
    window.thumbnails.wait()


class _Resizes(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize:
            self.count += 1
        return False


def _page(qtbot: QtBot, window: MainWindow, session: KeepSession) -> DetailPage:
    page = _open(qtbot, window, session, "Aurora Studio (Reykjavík)")
    qtbot.waitUntil(page.note_view.more.isVisible, timeout=5000)
    return page


def test_the_expanded_note_settles(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    """Once the pictures are fitted to the new width (``PICTURES_DELAY`` after a resize),
    nothing moves any more. It kept moving on macOS and Ubuntu: the picture's height
    followed the width, which showed or hid the page's scroll bar, which changed the
    width…"""
    note = _page(qtbot, window, session).note_view
    note.more.click()
    resizes = _Resizes()
    note.body.installEventFilter(resizes)
    unsettled = []
    for width in range(1000, 1300, 9):
        for height in (700, 900):
            window.resize(width, height)
            qtbot.wait(PICTURES_DELAY + 100)
            resizes.count = 0
            qtbot.wait(100)
            if resizes.count:
                unsettled.append((width, height, resizes.count))
    assert unsettled == []


def test_dragging_the_width_quickly(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    """A drag, a frame at a time, narrower and wider again: this overflowed Qt's stack
    (a crash, so the run itself fails) while pictures followed the width as it changed."""
    note = _page(qtbot, window, session).note_view
    note.more.click()
    for low in (900, 650):
        for width in [*range(1300, low, -4), *range(low, 1300, 4)]:
            window.resize(width, 900)
            qtbot.wait(5)
    qtbot.wait(PICTURES_DELAY + 100)
    assert note.body.isVisible()


def test_show_more_fits_the_header(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _page(qtbot, window, session)
    header = page.splitter.widget(0)
    assert header is not None
    window.resize(1200, 1000)
    qtbot.waitUntil(lambda: header.height() == page._middle.sizeHint().height(), timeout=5000)
    collapsed = header.height()
    page.note_view.more.click()  # taller: the header grows, without resizing the window
    qtbot.waitUntil(lambda: header.height() > collapsed + 100, timeout=5000)
    page.note_view.more.click()
    try:  # back to fitting the collapsed note (a few pixels either way)
        qtbot.waitUntil(lambda: abs(header.height() - collapsed) < 10, timeout=5000)
    except Exception as e:
        hint = page._middle.sizeHint().height()
        raise AssertionError(f"header {header.height()}, was {collapsed}, wants {hint}") from e
