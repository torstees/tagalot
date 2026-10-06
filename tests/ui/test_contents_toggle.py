"""The Contents toggle in the filter bar (#350): offered when the keep searches inside
documents, it makes the search box find items by what their files say."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from pytestqt.qtbot import QtBot

from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.main_window import MainWindow
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import ScanController
from tests.core.book_files import paged_pdf
from tests.core.test_contents import THEME

pytestmark = pytest.mark.gui


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    themes, files = tmp_path / "themes", tmp_path / "files"
    themes.mkdir()
    files.mkdir()
    (themes / "docs.py").write_text(THEME, encoding="utf-8")
    (files / "botany.pdf").write_bytes(paged_pdf([["Leaves"], ["Photosynthesis needs light"]]))
    (files / "kitchen.txt").write_text("sugar in the cafe", encoding="utf-8")
    keep = create_keep(
        tmp_path / "Docs.keep", "Docs", ThemeRef("docs", 1), [RootConfig("f", "F", str(files))]
    )
    with KeepSession.open(keep.dir, Settings(theme_dirs=[themes])) as opened:
        yield opened


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.show()
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    return window


def _page(window: MainWindow) -> SearchPage:
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    return page


def test_contents_is_offered_only_when_the_keep_searches_documents(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page(window)
    assert page.filter_bar.contents_box.isHidden()
    session.set_contents_index("words")
    with qtbot.waitSignal(window._contents_done, timeout=10_000):
        window.update_contents()  # as the Keep tab does
    assert not page.filter_bar.contents_box.isHidden()
    session.set_contents_index(None)
    window.update_contents()
    assert page.filter_bar.contents_box.isHidden()


def test_the_toggle_finds_items_by_their_contents(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    session.set_contents_index("words")
    with qtbot.waitSignal(window._contents_done, timeout=10_000):
        window.update_contents()
    page = _page(window)
    page.filter_bar.set_text("photosynth")
    qtbot.waitUntil(lambda: page.status.text() == "Nothing found", timeout=5000)
    page.filter_bar.contents_box.setChecked(True)
    qtbot.waitUntil(lambda: page.status.text() == "1 item", timeout=5000)
    assert page.current_spec().contents is True
    saved = page.saved_definition()
    assert saved.filters.contents is True  # kept with a saved search
