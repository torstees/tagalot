"""The Tag manager's tag groups (#536): a tag's groups in the details pane, changed there;
the Groups menu listing what each group holds, renaming and deleting groups (their tags
stay), and making one; every change undoable."""

import pytest
from PySide6.QtCore import QModelIndex
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.ui import tag_manager
from tagalot.ui.main_window import MainWindow
from tagalot.ui.tag_manager import TagManagerPage
from tests.ui.test_tag_manager import _manager, _select, session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


def _action(menu: QMenu, text: str) -> QAction:
    [action] = [a for a in menu.actions() if a.text() == text]
    return action


def _groups(session: KeepSession) -> dict[str, set[str]]:
    tree = session.tag_cache.get()
    return {g.name: {tree.node(t).name for t in g.tags} for g in tree.groups()}


def _status(qtbot: QtBot, window: MainWindow, expected: str) -> None:
    def check() -> None:
        assert window.statusBar().currentMessage() == expected

    qtbot.waitUntil(check, timeout=5000)


def test_the_details_pane_shows_and_changes_a_tags_groups(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _manager(window)
    details = page.details
    _select(page, "Iceland")
    assert details.groups.text() == "None"

    monkeypatch.setattr(tag_manager, "ask_name", lambda *a: "Trips")
    details._fill_groups_menu()
    _action(details.groups_menu, "New group…").trigger()
    _status(qtbot, window, "Made the group 'Trips' with 'Iceland'.")
    qtbot.waitUntil(lambda: details.groups.text() == "Trips", timeout=5000)

    _select(page, "Places")
    details._fill_groups_menu()
    trips = _action(details.groups_menu, "Trips")
    assert not trips.isChecked()
    trips.trigger()  # ticks it: Places goes in
    _status(qtbot, window, "Added 'Places' to the group 'Trips'.")
    _select(page, "Beach")
    qtbot.waitUntil(lambda: details.groups.text() == "through 'Places': Trips", timeout=5000)
    _select(page, "Iceland")
    assert details.groups.text() == "Trips; through 'Places': Trips"

    details._fill_groups_menu()
    _action(details.groups_menu, "Trips").trigger()  # unticks it: Iceland comes out
    _status(qtbot, window, "Took 'Iceland' out of the group 'Trips'.")
    assert _groups(session) == {"Trips": {"Places"}}


def test_the_groups_menu_renames_deletes_and_makes_groups(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = session.tag_cache.get()
    tops = {tree.node(t).name: t for t in tree.children(None)}
    session.tags.add_group("Trips", [tops["Places"]])
    page: TagManagerPage = _manager(window)
    page.reload()  # made after the page loaded
    qtbot.waitUntil(lambda: bool(page.model.tree and page.model.tree.groups()), timeout=5000)
    page._fill_groups_menu()
    [trips] = [a.menu() for a in page.groups_menu.actions() if a.menu() is not None]
    assert isinstance(trips, QMenu)
    assert trips.title() == "Trips (1 tag)"
    listed = [a.text() for a in trips.actions() if not a.isSeparator()]
    assert listed == ["Places", "Rename…", "Delete"]

    monkeypatch.setattr(tag_manager, "ask_name", lambda *a: "Travel")
    _action(trips, "Rename…").trigger()
    _status(qtbot, window, "Renamed the group 'Trips' to 'Travel'.")
    assert _groups(session) == {"Travel": {"Places"}}
    qtbot.waitUntil(  # the page reloads after the change
        lambda: (
            page.model.tree is not None and [g.name for g in page.model.tree.groups()] == ["Travel"]
        ),
        timeout=5000,
    )

    page._fill_groups_menu()
    [travel] = [a.menu() for a in page.groups_menu.actions() if a.menu() is not None]
    assert isinstance(travel, QMenu)
    _action(travel, "Delete").trigger()
    _status(qtbot, window, "Deleted the group 'Travel'; its tags stay.")
    assert _groups(session) == {}
    assert "Places" in {tree.node(t).name for t in session.tag_cache.get().children(None)}

    window.undo_action.trigger()  # the group comes back, with its tag
    qtbot.waitUntil(lambda: _groups(session) == {"Travel": {"Places"}}, timeout=5000)

    page.view.setCurrentIndex(QModelIndex())  # no tag selected: an empty group
    monkeypatch.setattr(tag_manager, "ask_name", lambda *a: "Everyday")
    page._fill_groups_menu()
    _action(page.groups_menu, "New group…").trigger()
    _status(qtbot, window, "Made the group 'Everyday'.")
    assert _groups(session) == {"Travel": {"Places"}, "Everyday": set()}
    qtbot.waitUntil(lambda: page.model.tree is not None and len(page.model.tree.groups()) == 2)
    page._fill_groups_menu()
    menus = [a.menu() for a in page.groups_menu.actions() if a.menu() is not None]
    assert [m.title() for m in menus if isinstance(m, QMenu)] == [
        "Travel (1 tag)",
        "Everyday (0 tags)",
    ]
    empty = menus[1]
    assert isinstance(empty, QMenu)
    assert next(a.text() for a in empty.actions() if not a.isSeparator()) == "No tags yet"
