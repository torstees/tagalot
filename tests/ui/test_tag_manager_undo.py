"""Tests for Undo / Redo in the Tag manager, on the session's history (#71)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, Qt, QThreadPool
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.dnd import tags_mime
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.tag_manager import TagManagerPage
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
    window.navigation.select(NavTarget("tags", label="Tag manager"))
    qtbot.waitUntil(lambda: _page(window).loaded, timeout=5000)
    qtbot.waitUntil(lambda: window.tag_panel.loaded, timeout=5000)
    return window


def _page(window: MainWindow) -> TagManagerPage:
    page = window.stack.currentWidget()
    assert isinstance(page, TagManagerPage)
    return page


def _id(session: KeepSession, *path: str) -> int | None:
    tree = session.tag_cache.get()
    parent: int | None = None
    for name in path:
        parent = tree.find_child(parent, name)
        if parent is None:
            return None
    return parent


def _tree_state(session: KeepSession) -> set[tuple[str, ...]]:
    tree = session.tag_cache.get()
    return {(*tree.path(t), tree.node(t).color or "") for t in tree}


def _wait_status(qtbot: QtBot, window: MainWindow, prefix: str) -> None:
    def check() -> None:
        assert window.statusBar().currentMessage().startswith(prefix)

    qtbot.waitUntil(check, timeout=5000)


def test_the_buttons_follow_the_history(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page(window)
    assert not page.undo_button.isEnabled()
    assert not page.redo_button.isEnabled()
    assert page.undo_button.toolTip() == "Nothing to undo"

    beach = _id(session, "Places", "Beach")
    assert beach is not None
    page.model.setData(page.model.index_of(beach), "Seaside", Qt.ItemDataRole.EditRole)
    _wait_status(qtbot, window, "Renamed")
    assert page.undo_button.isEnabled()
    assert page.undo_button.toolTip() == "Undo: Rename 'Beach' to 'Seaside'"

    page.undo_button.click()
    _wait_status(qtbot, window, "Undid: Rename 'Beach' to 'Seaside'.")
    assert _id(session, "Places", "Beach") == beach
    assert page.redo_button.toolTip() == "Redo: Rename 'Beach' to 'Seaside'"
    assert not page.undo_button.isEnabled()

    page.redo_button.click()
    _wait_status(qtbot, window, "Redid:")
    assert _id(session, "Places", "Seaside") == beach


def test_a_sequence_undoes_in_reverse_and_redoes(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page(window)
    before = _tree_state(session)
    beach, topics = _id(session, "Places", "Beach"), _id(session, "Topics")
    sky = _id(session, "Topics", "Sky")
    assert beach is not None
    assert topics is not None
    assert sky is not None

    page.model.setData(page.model.index_of(beach), "Seaside", Qt.ItemDataRole.EditRole)
    _wait_status(qtbot, window, "Renamed")
    page.model.dropMimeData(
        tags_mime([beach]), Qt.DropAction.MoveAction, -1, 0, page.model.index_of(topics)
    )
    _wait_status(qtbot, window, "Moved")
    page.details.color_chosen.emit(sky, "#aa3355")
    _wait_status(qtbot, window, "Set the color")
    after = _tree_state(session)
    assert after != before

    # One step at a time, each waiting until its change is written (the history's labels
    # change a moment earlier, when the step is taken off the stack).
    for _ in range(3):
        with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
            page.undo_button.click()
    assert _tree_state(session) == before
    assert not page.undo_button.isEnabled()

    for _ in range(3):
        with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
            page.redo_button.click()
    assert _tree_state(session) == after
    assert not page.redo_button.isEnabled()


def test_tagging_shares_the_history(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _page(window)
    favorites = _id(session, "Favorites")
    assert favorites is not None
    window.apply_tags([1], [favorites])  # as from a search page
    qtbot.waitUntil(page.undo_button.isEnabled, timeout=5000)
    assert page.undo_button.toolTip() == "Undo: Tag 1 item with 'Favorites'"


def test_the_selection_is_kept_through_an_undo(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page(window)
    iceland, beach = _id(session, "Places", "Iceland"), _id(session, "Places", "Beach")
    assert iceland is not None
    assert beach is not None
    page.select_tag(iceland)
    page.model.setData(page.model.index_of(beach), "Seaside", Qt.ItemDataRole.EditRole)
    _wait_status(qtbot, window, "Renamed")
    page.undo_button.click()
    _wait_status(qtbot, window, "Undid:")
    qtbot.waitUntil(lambda: page.model.index_of(beach).data() == "Beach", timeout=5000)
    assert page.current_tag() == iceland
    assert page.details.title.text() == "Iceland"
    assert page.view.currentIndex() != QModelIndex()
