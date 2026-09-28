"""Tests for adding, renaming, and moving tags in the Tag manager (#68)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, QPointF, Qt, QThreadPool
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QDialog
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui import tag_manager
from tagalot.ui.dnd import tags_mime
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.tag_manager import TagManagerPage
from tagalot.ui.tag_picker import TagPickerDialog, move_dialog
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


def _wait_status(qtbot: QtBot, window: MainWindow, expected: str) -> None:
    def check() -> None:
        assert window.statusBar().currentMessage() == expected

    qtbot.waitUntil(check, timeout=5000)


def test_new_tag_and_new_sub_tag(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(window)
    asked: list[str] = []

    def answer(parent: object, title: str, label: str, text: str = "") -> str:
        asked.append(label)
        return "Norway" if "Places" in label else "Events"

    monkeypatch.setattr(tag_manager, "ask_name", answer)
    assert not page.new_child_button.isEnabled()  # nothing selected yet
    page.new_button.click()
    _wait_status(qtbot, window, "Added tag 'Events'.")
    assert _id(session, "Events") is not None

    places = _id(session, "Places")
    assert places is not None
    page.select_tag(places)
    page.new_child_button.click()
    _wait_status(qtbot, window, "Added tag 'Norway'.")
    assert _id(session, "Places", "Norway") is not None
    assert asked == [
        "Name of the new tag at the top level:",
        "Name of the new tag under “Places”:",
    ]


def test_rename_in_place(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _page(window)
    beach = _id(session, "Places", "Beach")
    assert beach is not None
    page.model.setData(page.model.index_of(beach), "Seaside", Qt.ItemDataRole.EditRole)
    _wait_status(qtbot, window, "Renamed 'Beach' to 'Seaside'.")
    assert _id(session, "Places", "Seaside") == beach
    qtbot.waitUntil(lambda: page.model.index_of(beach).data() == "Seaside", timeout=5000)

    page.model.setData(
        page.model.index_of(beach), "Iceland", Qt.ItemDataRole.EditRole
    )  # a sibling's
    qtbot.waitUntil(lambda: "already" in window.statusBar().currentMessage(), timeout=5000)
    assert _id(session, "Places", "Seaside") == beach  # unchanged


def test_move_by_dropping(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _page(window)
    beach, topics = _id(session, "Places", "Beach"), _id(session, "Topics")
    assert beach is not None
    assert topics is not None
    action = Qt.DropAction.MoveAction
    page.model.dropMimeData(tags_mime([beach]), action, -1, 0, page.model.index_of(topics))
    _wait_status(qtbot, window, "Moved 'Beach' under 'Topics' (2 items).")
    assert _id(session, "Topics", "Beach") == beach

    page.model.dropMimeData(tags_mime([beach]), action, -1, 0, QModelIndex())  # empty space
    _wait_status(qtbot, window, "Moved 'Beach' to the top level (2 items).")
    assert _id(session, "Beach") == beach

    places = _id(session, "Places")
    assert places is not None
    page.model.dropMimeData(
        tags_mime([places]),
        action,
        -1,
        0,
        page.model.index_of(_id(session, "Places", "Iceland") or 0),
    )
    qtbot.waitUntil(lambda: "under itself" in window.statusBar().currentMessage(), timeout=5000)

    window.undo_action.trigger()  # undoes the move to the top level
    qtbot.waitUntil(lambda: _id(session, "Topics", "Beach") == beach, timeout=5000)


def test_move_to_picker(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(window)
    money, people = _id(session, "Topics", "Money"), _id(session, "People")
    assert money is not None
    assert people is not None

    def pick(dialog: TagPickerDialog) -> QDialog.DialogCode:
        dialog.choose(people)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(TagPickerDialog, "exec", pick)
    page.select_tag(money)
    page.move_button.click()
    _wait_status(qtbot, window, "Moved 'Money' under 'People' (2 items).")
    assert _id(session, "People", "Money") == money


def test_the_picker_blocks_the_tag_and_its_sub_tags(qtbot: QtBot, session: KeepSession) -> None:
    tree = session.tag_cache.get()
    places = _id(session, "Places")
    iceland, topics = _id(session, "Places", "Iceland"), _id(session, "Topics")
    assert places is not None
    assert iceland is not None
    dialog = move_dialog(tree, places)
    qtbot.addWidget(dialog)
    ok = dialog.ok_button
    assert not dialog.model.flags(dialog.model.index_of(iceland))  # its own sub-tag
    assert not dialog.model.flags(dialog.model.index_of(places))
    assert not ok.isEnabled()
    assert dialog.message.text() == "Choose a tag."
    dialog.choose(None)
    assert dialog.message.text() == "It's already there."  # Places is at the top level
    assert not ok.isEnabled()
    dialog.choose(topics)
    assert dialog.target() == topics
    assert ok.isEnabled()
    dialog.filter_edit.setText("fam")
    assert dialog.model.rowCount() == 1  # People, holding Family


def _drag(page: TagManagerPage, source: int, target: int, *, drop: bool) -> bool:
    """Drag ``source`` over ``target`` the way Qt does (enter, move, and maybe drop, on the
    tree's viewport); returns whether the tree accepted the drag there."""
    view, model = page.view, page.model
    mime = model.mimeData([model.index_of(source)])  # kept alive while the events use it
    actions = model.supportedDragActions()
    where = view.visualRect(model.index_of(target)).center()
    left, none = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    QApplication.sendEvent(view.viewport(), QDragEnterEvent(where, actions, mime, left, none))
    move = QDragMoveEvent(where, actions, mime, left, none)
    QApplication.sendEvent(view.viewport(), move)
    if drop and move.isAccepted():
        QApplication.sendEvent(
            view.viewport(), QDropEvent(QPointF(where), move.dropAction(), mime, left, none)
        )
    return move.isAccepted()


def test_dragging_in_the_tree_moves_the_tag(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    """Through the view, as a mouse drag goes: the tree must accept its own tags (it
    refused them once, showing the "no" cursor, because the model offered only copying)."""
    page = _page(window)
    beach, topics = _id(session, "Places", "Beach"), _id(session, "Topics")
    assert beach is not None
    assert topics is not None
    assert _drag(page, beach, topics, drop=True)
    _wait_status(qtbot, window, "Moved 'Beach' under 'Topics' (2 items).")
    assert _id(session, "Topics", "Beach") == beach
