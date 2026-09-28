"""Tests for the Tags panel's right-click menu (#65)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, QModelIndex, QThreadPool
from PySide6.QtGui import QAction
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.filter_bar import Chip
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
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


def _tag(window: MainWindow, name: str) -> int:
    tree = window.tag_panel.model.tree
    assert tree is not None
    [tag_id] = [s.tag_id for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
    return tag_id


def _menu(window: MainWindow, name: str) -> dict[str, QAction]:
    panel = window.tag_panel
    menu = panel.menu_for(panel.model.index_of(_tag(window, name)))
    assert menu is not None
    return {a.text(): a for a in menu.actions() if not a.isSeparator()}


def _chips(page: SearchPage) -> list[str]:
    return sorted(c.label.text() for c in page.filter_bar.findChildren(Chip))


def test_the_menu_for_one_tag(window: MainWindow) -> None:
    actions = _menu(window, "Iceland")
    assert list(actions) == [
        "Show only items with 'Iceland'",
        "Hide items with 'Iceland'",
        "Add 'Iceland' to the selected items",
        "Remove 'Iceland' from the selected items",
    ]
    assert not actions["Add 'Iceland' to the selected items"].isEnabled()  # none selected
    assert window.tag_panel.menu_for(QModelIndex()) is None  # off the tags


def test_show_only_and_hide_add_chips(qtbot: QtBot, window: MainWindow) -> None:
    page = _page(window)
    _menu(window, "Places")["Show only items with 'Places'"].trigger()
    qtbot.waitUntil(lambda: page.status.text() == "5 items", timeout=5000)
    _menu(window, "Beach")["Hide items with 'Beach'"].trigger()
    qtbot.waitUntil(lambda: page.status.text() == "3 items", timeout=5000)
    assert _chips(page) == ["Places", "not: Beach"]
    assert window.statusBar().currentMessage() == "Hiding items with 'Beach'."


def test_the_menu_covers_every_selected_tag(qtbot: QtBot, window: MainWindow) -> None:
    panel = window.tag_panel
    for name in ("Money", "Sky"):
        panel.view.selectionModel().select(
            panel.model.index_of(_tag(window, name)), QItemSelectionModel.SelectionFlag.Select
        )
    actions = _menu(window, "Sky")  # one of the selected tags: the menu is for both
    actions["Show only items with 2 tags"].trigger()
    qtbot.waitUntil(lambda: _page(window).status.text() == "Nothing found", timeout=5000)
    assert _chips(_page(window)) == ["Money", "Sky"]
    assert "Show only items with 'Iceland'" in _menu(window, "Iceland")  # not selected


def test_add_and_remove_act_on_the_selected_items(qtbot: QtBot, window: MainWindow) -> None:
    page = _page(window)
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    for row in range(page.model.rowCount()):
        if page.model.index(row, 0).data() in ("notes.txt", "readme.md"):
            page.table.selectionModel().select(page.model.index(row, 0), flags)
    qtbot.waitUntil(lambda: window.tag_panel.model.selected_count == 2, timeout=5000)
    add = _menu(window, "Sky")["Add 'Sky' to the 2 selected items"]
    assert add.isEnabled()
    add.trigger()
    qtbot.waitUntil(
        lambda: window.statusBar().currentMessage() == "Tagged 2 items with 'Sky'.", timeout=5000
    )
    _menu(window, "Sky")["Remove 'Sky' from the 2 selected items"].trigger()
    qtbot.waitUntil(
        lambda: window.statusBar().currentMessage() == "Removed 'Sky' from 2 items.", timeout=5000
    )


def test_from_another_page_the_search_opens(qtbot: QtBot, window: MainWindow) -> None:
    window.navigation.select(NavTarget("triage", label="Triage"))
    assert not isinstance(window.stack.currentWidget(), SearchPage)
    _menu(window, "Money")["Show only items with 'Money'"].trigger()
    page = _page(window)
    assert page.grouped  # Search all
    qtbot.waitUntil(lambda: page.status.text() == "2 items", timeout=5000)
