"""Tests for tagging from the keyboard (DESIGN.md §12 "Tagging panel", #62)."""

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
from tagalot.ui.main_window import MainWindow
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
SHIFT = Qt.KeyboardModifier.ShiftModifier


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
    """Wait for the status bar to say ``expected``; a timeout reports what it said."""

    def check() -> None:
        assert _status(window) == expected

    qtbot.waitUntil(check, timeout=5000)


def _tagged(session: KeepSession, name: str) -> set[str]:
    tree = session.tag_cache.get()
    [tag_id] = [s.tag_id for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
    with session.reader.connect() as conn:
        return {h.title for h in run_search(conn, SearchSpec(include=(tag_id,)), tree)}


def test_type_then_enter_applies_and_shift_enter_removes(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    _select(window, "notes.txt", "readme.md")
    panel = window.tag_panel
    QTest.keyClicks(panel.filter_edit, "sky")
    QTest.keyClick(panel.filter_edit, Qt.Key.Key_Return)
    _wait_status(qtbot, window, "Tagged 2 items with 'Sky'.")
    assert {"notes.txt", "readme.md"} <= _tagged(session, "Sky")
    assert panel.filter_edit.selectedText() == "sky"  # typing the next tag replaces it

    QTest.keyClick(panel.filter_edit, Qt.Key.Key_Return, SHIFT)
    _wait_status(qtbot, window, "Removed 'Sky' from 2 items.")
    assert not {"notes.txt", "readme.md"} & _tagged(session, "Sky")


def test_enter_without_a_selection_explains(qtbot: QtBot, window: MainWindow) -> None:
    QTest.keyClicks(window.tag_panel.filter_edit, "money")
    QTest.keyClick(window.tag_panel.filter_edit, Qt.Key.Key_Return)
    qtbot.waitUntil(
        lambda: _status(window) == "Select items to tag with 'Money' first.", timeout=5000
    )
    assert window.tag_actions.undo_label is None


def test_enter_in_the_tree_applies_every_selected_tag(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    _select(window, "readme.md")
    panel = window.tag_panel
    tree = panel.model.tree
    assert tree is not None
    flags = QItemSelectionModel.SelectionFlag.Select
    for name in ("Money", "Favorites"):
        [tag_id] = [s.tag_id for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
        panel.view.selectionModel().select(panel.model.index_of(tag_id), flags)
    QTest.keyClick(panel.view, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: _status(window).startswith("Tagged 1 item with"), timeout=5000)
    assert "readme.md" in _tagged(session, "Money") & _tagged(session, "Favorites")


def test_typing_in_the_tree_filters(qtbot: QtBot, window: MainWindow) -> None:
    panel = window.tag_panel
    panel.view.setFocus()
    QTest.keyClick(panel.view, Qt.Key.Key_M)
    QTest.keyClicks(panel.filter_edit, "on")
    assert panel.filter_edit.text() == "mon"
    assert panel.model.filtering
    panel.view.setFocus()
    QTest.keyClick(panel.view, Qt.Key.Key_Escape)  # back to the filter box
    assert panel.filter_edit.hasFocus()


def test_ctrl_t_goes_to_the_tag_filter(qtbot: QtBot, window: MainWindow) -> None:
    window.tags_dock.hide()
    window.tag_panel.filter_edit.setText("old")
    assert window.tag_selection_action.shortcut().toString() == "Ctrl+T"
    window.tag_selection_action.trigger()
    assert window.tags_dock.isVisible()
    assert window.tag_panel.filter_edit.hasFocus()
    assert window.tag_panel.filter_edit.selectedText() == "old"
