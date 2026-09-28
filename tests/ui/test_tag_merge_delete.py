"""Tests for merging and deleting tags in the Tag manager (#69)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QDialog
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import PATH_SEPARATOR, DeleteMode, TagUsage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.tag_manager import TagManagerPage
from tagalot.ui.tag_picker import DeleteDialog, TagPickerDialog, merge_dialog
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


# --- the dialogs ---


def test_the_merge_dialog_explains_what_happens(qtbot: QtBot, session: KeepSession) -> None:
    tree = session.tag_cache.get()
    places, beach = _id(session, "Places"), _id(session, "Places", "Beach")
    iceland = _id(session, "Places", "Iceland")
    assert places is not None
    assert beach is not None
    assert iceland is not None
    dialog = merge_dialog(tree, places, TagUsage(0, 5))
    qtbot.addWidget(dialog)
    assert not dialog.model.flags(dialog.model.index_of(beach))  # its own sub-tag
    assert not dialog.top_level.isVisibleTo(dialog)  # merging needs a tag
    assert not dialog.ok_button.isEnabled()

    dialog = merge_dialog(tree, beach, TagUsage(2, 2))
    qtbot.addWidget(dialog)
    dialog.choose(iceland)
    assert dialog.ok_button.isEnabled()
    assert dialog.explanation.text() == (
        f"2 items tagged “Beach” will be tagged “Places{PATH_SEPARATOR}Iceland” "
        "instead. “Beach” becomes another name for it, and is then deleted."
    )


def test_the_delete_dialog_counts_what_is_lost(qtbot: QtBot, session: KeepSession) -> None:
    tree = session.tag_cache.get()
    places, sky = _id(session, "Places"), _id(session, "Topics", "Sky")
    assert places is not None
    assert sky is not None
    leaf = DeleteDialog(tree, sky, TagUsage(2, 2))
    qtbot.addWidget(leaf)
    assert leaf.mode() is None
    assert not leaf.subtree.isVisibleTo(leaf)
    assert leaf.effect.text() == "2 items will lose a tag."

    parent = DeleteDialog(tree, places, TagUsage(0, 5))
    qtbot.addWidget(parent)
    assert parent.subtree.text() == "Delete “Places” and its 2 sub-tags"
    assert parent.promote.text() == "Delete only “Places”; move its sub-tags up to the top level"
    assert parent.mode() is DeleteMode.SUBTREE
    assert parent.effect.text() == "5 items will lose a tag."
    parent.promote.setChecked(True)
    assert parent.mode() is DeleteMode.PROMOTE
    assert parent.effect.text() == "No items use it."


# --- in the Tag manager ---


def test_merging(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(window)
    beach, iceland = _id(session, "Places", "Beach"), _id(session, "Places", "Iceland")
    assert beach is not None
    assert iceland is not None

    def pick(dialog: TagPickerDialog) -> QDialog.DialogCode:
        dialog.choose(iceland)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(TagPickerDialog, "exec", pick)
    page.select_tag(beach)
    page.merge_button.click()
    _wait_status(qtbot, window, "Merged 'Beach' into 'Iceland' (2 items retagged).")
    tree = session.tag_cache.get()
    assert beach not in tree
    assert set(tree.aliases(iceland)) == {"Ísland", "Beach"}  # its old name is kept
    qtbot.waitUntil(lambda: page.model.usage.get(iceland) == TagUsage(5, 5), timeout=5000)

    window.undo_action.trigger()
    qtbot.waitUntil(lambda: _id(session, "Places", "Beach") == beach, timeout=5000)


@pytest.mark.parametrize(
    ("mode", "message", "left"),
    [
        (
            DeleteMode.SUBTREE,
            "Deleted 'Places' and its sub-tags (5 items lost a tag).",
            set(),
        ),
        (
            DeleteMode.PROMOTE,
            "Deleted 'Places' (0 items lost a tag).",
            {"Iceland", "Beach"},  # moved up to the top level
        ),
    ],
)
def test_deleting_a_tag_with_sub_tags(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    monkeypatch: pytest.MonkeyPatch,
    mode: DeleteMode,
    message: str,
    left: set[str],
) -> None:
    page = _page(window)
    places = _id(session, "Places")
    assert places is not None

    def confirm(dialog: DeleteDialog) -> QDialog.DialogCode:
        (dialog.subtree if mode is DeleteMode.SUBTREE else dialog.promote).setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(DeleteDialog, "exec", confirm)
    page.select_tag(places)
    page.delete_button.click()
    _wait_status(qtbot, window, message)
    tree = session.tag_cache.get()
    top = {tree.node(t).name for t in tree.children(None)}
    assert "Places" not in top
    assert {"Iceland", "Beach"} & top == left

    window.undo_action.trigger()
    qtbot.waitUntil(lambda: _id(session, "Places", "Iceland") is not None, timeout=5000)


def test_cancelling_changes_nothing(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(window)
    sky = _id(session, "Topics", "Sky")
    assert sky is not None
    monkeypatch.setattr(DeleteDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)
    page.select_tag(sky)
    page.delete_button.click()
    qtbot.wait(200)
    assert _id(session, "Topics", "Sky") == sky
    assert window.tag_actions.undo_label is None
