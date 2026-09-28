"""Tests for the Tags panel's all / some / none summary of the selection (#63)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt, QThreadPool
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import TagNode, TagTree
from tagalot.ui.main_window import MainWindow
from tagalot.ui.models.tag_tree import TagTreeModel
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
CHECKED, PARTIAL, UNCHECKED = (
    Qt.CheckState.Checked,
    Qt.CheckState.PartiallyChecked,
    Qt.CheckState.Unchecked,
)


def test_the_model_checks_all_some_none(qapp: object) -> None:
    model = TagTreeModel()
    model.set_tree(
        TagTree([TagNode(i, None, n, None, i) for i, n in [(1, "A"), (2, "B"), (3, "C")]], {})
    )
    a, b, c = (model.index_of(i) for i in (1, 2, 3))
    assert a.data(Qt.ItemDataRole.CheckStateRole) is None  # nothing selected: no checks
    assert not model.flags(a) & Qt.ItemFlag.ItemIsUserCheckable

    model.set_selection(3, {1: 3, 2: 1})
    assert [x.data(Qt.ItemDataRole.CheckStateRole) for x in (a, b, c)] == [
        CHECKED,
        PARTIAL,
        UNCHECKED,
    ]
    assert model.flags(a) & Qt.ItemFlag.ItemIsUserCheckable
    assert b.data(Qt.ItemDataRole.ToolTipRole).endswith("On 1 of the 3 selected items")

    clicks: list[tuple[int, bool]] = []
    model.check_clicked.connect(lambda tag_id, remove: clicks.append((tag_id, remove)))
    for index in (a, b, c):
        assert not model.setData(index, UNCHECKED, Qt.ItemDataRole.CheckStateRole)
    assert clicks == [(1, True), (2, False), (3, False)]  # all: remove; some or none: apply
    assert a.data(Qt.ItemDataRole.CheckStateRole) == CHECKED  # unchanged until retagged

    model.set_selection(0, {1: 3})
    assert a.data(Qt.ItemDataRole.CheckStateRole) is None


# --- in a keep window ---


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
    page.table.clearSelection()
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    for row in range(page.model.rowCount()):
        if page.model.index(row, 0).data() in titles:
            page.table.selectionModel().select(page.model.index(row, 0), flags)


def _state(window: MainWindow, name: str) -> Qt.CheckState | None:
    model = window.tag_panel.model
    tree = model.tree
    assert tree is not None
    [tag_id] = [s.tag_id for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
    return model.check_state(tag_id)


def _click(window: MainWindow, name: str) -> None:
    model = window.tag_panel.model
    tree = model.tree
    assert tree is not None
    [tag_id] = [s.tag_id for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
    model.setData(model.index_of(tag_id), UNCHECKED, Qt.ItemDataRole.CheckStateRole)


def test_the_summary_follows_the_selection(qtbot: QtBot, window: MainWindow) -> None:
    assert _state(window, "Iceland") is None
    _select(window, "glacier.jpg", "geyser.jpg")
    qtbot.waitUntil(lambda: _state(window, "Iceland") == CHECKED, timeout=5000)
    assert _state(window, "Favorites") == PARTIAL  # glacier only
    assert _state(window, "Beach") == UNCHECKED
    assert _state(window, "Places") == UNCHECKED  # only tags applied directly count
    label = window.tag_panel.selection_label
    assert label.isVisible()
    assert label.text().startswith("2 items selected.")

    _page(window).table.clearSelection()
    qtbot.waitUntil(lambda: _state(window, "Iceland") is None, timeout=5000)
    assert not label.isVisible()


def test_clicking_a_check_applies_to_all_or_removes_from_all(
    qtbot: QtBot, window: MainWindow
) -> None:
    _select(window, "glacier.jpg", "geyser.jpg")
    qtbot.waitUntil(lambda: _state(window, "Favorites") == PARTIAL, timeout=5000)
    _click(window, "Favorites")  # some: put it on all of them
    qtbot.waitUntil(lambda: _state(window, "Favorites") == CHECKED, timeout=5000)
    assert window.statusBar().currentMessage() == "Tagged 1 item with 'Favorites'."

    _click(window, "Iceland")  # all: take it off all of them
    qtbot.waitUntil(lambda: _state(window, "Iceland") == UNCHECKED, timeout=5000)
    assert window.statusBar().currentMessage() == "Removed 'Iceland' from 2 items."

    window.undo_action.trigger()  # the summary follows undo too
    qtbot.waitUntil(lambda: _state(window, "Iceland") == CHECKED, timeout=5000)


def test_switching_pages_updates_the_summary(qtbot: QtBot, window: MainWindow) -> None:
    _select(window, "tax 2025.pdf")
    qtbot.waitUntil(lambda: _state(window, "Money") == CHECKED, timeout=5000)
    files = next(t for t in window.navigation.targets("searches") if t.label == "Files")
    window.navigation.select(files)  # a new page: nothing selected there
    qtbot.waitUntil(lambda: _state(window, "Money") is None, timeout=5000)


def test_space_on_a_highlighted_tag_toggles_it(qtbot: QtBot, window: MainWindow) -> None:
    _select(window, "glacier.jpg", "geyser.jpg")
    qtbot.waitUntil(lambda: _state(window, "Favorites") == PARTIAL, timeout=5000)
    panel = window.tag_panel
    tree = panel.model.tree
    assert tree is not None
    [favorites] = [
        s.tag_id for s in tree.suggest("Favorites") if tree.node(s.tag_id).name == "Favorites"
    ]
    panel.view.setFocus()
    panel.view.setCurrentIndex(panel.model.index_of(favorites))
    QTest.keyClick(panel.view, Qt.Key.Key_Space)
    qtbot.waitUntil(lambda: _state(window, "Favorites") == CHECKED, timeout=5000)
