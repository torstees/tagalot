"""Help → Keyboard shortcuts (#128): every menu command's keys, the keys that work in
place, and no two commands on one key."""

import pytest
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenu
from pytestqt.qtbot import QtBot

from tagalot.ui.main_window import MainWindow
from tagalot.ui.shortcuts import IN_PLACE, ShortcutsDialog, keys_text
from tests.ui.test_contents_search import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

NATIVE = QKeySequence.SequenceFormat.NativeText


def _menu_actions(menu: QMenu) -> list[QAction]:
    found = []
    for action in menu.actions():
        sub = action.menu()
        if isinstance(sub, QMenu):
            found += _menu_actions(sub)
        elif not action.isSeparator():
            found.append(action)
    return found


def _all_menu_actions(window: MainWindow) -> list[QAction]:
    found = []
    for top in window.menuBar().actions():
        menu = top.menu()
        if isinstance(menu, QMenu):
            found += _menu_actions(menu)
    return found


def test_help_opens_the_list(qtbot: QtBot, window: MainWindow) -> None:
    action = window.shortcuts_action
    assert [k.toString() for k in action.shortcuts()] == ["F1", "Ctrl+/"]
    assert action in _all_menu_actions(window)
    dialog = window.show_shortcuts()
    qtbot.addWidget(dialog)
    assert isinstance(dialog, ShortcutsDialog)
    assert dialog.isVisible()
    assert not dialog.isModal()  # it can stay open beside the work


def test_every_menu_key_is_listed(qtbot: QtBot, window: MainWindow) -> None:
    dialog = ShortcutsDialog(window)
    qtbot.addWidget(dialog)
    rows = dialog.rows()
    keyed = [a for a in _all_menu_actions(window) if a.shortcuts()]
    assert len(keyed) >= 10
    listed = {(keys, what) for _, keys, what in rows}
    for action in keyed:
        keys = ", ".join(dict.fromkeys(k.toString(NATIVE) for k in action.shortcuts()))
        what = action.text().replace("&", "").rstrip("…").strip()
        assert (keys, what) in listed, (keys, what)
    save = QKeySequence(QKeySequence.StandardKey.Save).toString(NATIVE)
    assert ("Menu: Edit", save, "Save search") in rows
    assert ("Menu: Keep", "F5", "Scan now") in rows


def test_keys_that_work_in_place_are_listed(qtbot: QtBot, window: MainWindow) -> None:
    dialog = ShortcutsDialog(window)
    qtbot.addWidget(dialog)
    rows = dialog.rows()
    for group, entries in IN_PLACE:
        for keys, what in entries:
            assert (group, keys_text(keys), what) in rows
    assert keys_text("Double-click") == "Double-click"
    assert keys_text("Up, Down") == "Up, Down"
    assert keys_text("Ctrl+Enter") == QKeySequence("Ctrl+Enter").toString(NATIVE)


def test_narrowing_the_list(qtbot: QtBot, window: MainWindow) -> None:
    dialog = ShortcutsDialog(window)
    qtbot.addWidget(dialog)
    everything = len(dialog.rows())
    dialog.search_box.setText("but not")
    assert [what for _, _, what in dialog.rows()] == ["Add it as a “but not” chip"]
    dialog.search_box.setText("tags panel")  # a group's name shows all of it
    assert {group for group, _, _ in dialog.rows()} == {"Tags panel"}
    dialog.search_box.clear()
    assert len(dialog.rows()) == everything


def test_no_two_commands_share_a_key(window: MainWindow) -> None:
    """Qt fires neither of two actions on one key: each key belongs to one command."""
    owners: dict[str, str] = {}
    for action in window.findChildren(QAction):
        for key in action.shortcuts():
            text = key.toString()
            if not text:
                continue
            assert text not in owners or owners[text] == action.text(), (
                text,
                owners.get(text),
                action.text(),
            )
            owners[text] = action.text()
