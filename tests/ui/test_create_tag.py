"""Tests for "Create tag '…'" in the Tags panel (DESIGN.md §12, #64)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt, QThreadPool
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import PATH_SEPARATOR
from tagalot.ui.main_window import MainWindow
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_demo(tmp_path), Settings()) as session:
        yield session


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()
    qtbot.waitUntil(lambda: _page(window).status.text() == "9 items", timeout=5000)
    qtbot.waitUntil(lambda: window.tag_panel.loaded, timeout=5000)
    return window


def _page(window: MainWindow) -> SearchPage:
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    return page


def _select(window: MainWindow, *titles: str) -> None:
    page = _page(window)
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    for row in range(page.model.rowCount()):
        if page.model.index(row, 0).data() in titles:
            page.table.selectionModel().select(page.model.index(row, 0), flags)


def _status(window: MainWindow) -> str:
    return window.statusBar().currentMessage()


def _wait_status(qtbot: QtBot, window: MainWindow, expected: str) -> None:
    def check() -> None:
        assert _status(window) == expected

    qtbot.waitUntil(check, timeout=5000)


def _find(session: KeepSession, *path: str) -> int | None:
    tree = session.tag_cache.get()
    parent: int | None = None
    for name in path:
        parent = tree.find_child(parent, name)
        if parent is None:
            return None
    return parent


def test_the_button_appears_only_when_nothing_matches(window: MainWindow) -> None:
    panel = window.tag_panel
    assert not panel.create_button.isVisible()
    panel.filter_edit.setText("ice")  # matches Iceland
    assert not panel.create_button.isVisible()
    panel.filter_edit.setText("  Norway  ")
    assert panel.create_button.isVisible()
    assert panel.create_button.text() == "Create tag “Norway”"
    assert panel.message.text() == "No tags match “Norway”."
    panel.filter_edit.clear()
    assert not panel.create_button.isVisible()


def test_enter_creates_a_top_level_tag(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    panel = window.tag_panel
    QTest.keyClicks(panel.filter_edit, "Norway")
    QTest.keyClick(panel.filter_edit, Qt.Key.Key_Return)
    _wait_status(qtbot, window, "Created tag 'Norway'.")
    assert _find(session, "Norway") is not None
    qtbot.waitUntil(lambda: not panel.create_button.isVisible(), timeout=5000)
    assert panel.model.matches() == {_find(session, "Norway")}  # the filter now finds it


def test_a_path_creates_under_a_parent_and_tags_the_selection(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    _select(window, "notes.txt", "readme.md")
    panel = window.tag_panel
    QTest.keyClicks(panel.filter_edit, "Places > Norway")
    qtbot.waitUntil(
        lambda: panel.create_button.text().endswith("and add it to 2 items"), timeout=5000
    )
    QTest.mouseClick(panel.create_button, Qt.MouseButton.LeftButton)
    _wait_status(
        qtbot, window, f"Created tag 'Places{PATH_SEPARATOR}Norway' and tagged 2 items with it."
    )
    norway = _find(session, "Places", "Norway")
    assert norway is not None
    tree = session.tag_cache.get()
    with session.reader.connect() as conn:
        tagged = {h.title for h in run_search(conn, SearchSpec(include=(norway,)), tree)}
    assert tagged == {"notes.txt", "readme.md"}

    window.undo_action.trigger()  # the tagging
    _wait_status(qtbot, window, "Undid: Tag 2 items with 'Norway'.")
    window.undo_action.trigger()  # the new tag
    qtbot.waitUntil(lambda: _find(session, "Places", "Norway") is None, timeout=5000)


def test_shift_enter_never_creates(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    panel = window.tag_panel
    QTest.keyClicks(panel.filter_edit, "Norway")
    QTest.keyClick(panel.filter_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    qtbot.wait(200)
    assert _find(session, "Norway") is None


def test_a_bad_name_is_explained(qtbot: QtBot, window: MainWindow) -> None:
    panel = window.tag_panel
    panel.filter_edit.setText("x" * 201)
    panel.request_create()
    _wait_status(qtbot, window, "A tag name can be at most 200 characters.")


def test_the_button_shows_the_path_it_will_create(window: MainWindow) -> None:
    panel = window.tag_panel
    panel.filter_edit.setText("Places > Norway")
    assert panel.create_button.text() == f"Create tag \u201cPlaces{PATH_SEPARATOR}Norway\u201d"
    assert not panel.view.isVisible()  # nothing to show: the message sits under the filter
    panel.filter_edit.clear()
    assert panel.view.isVisible()
