"""Tests for editing a tag's color, description, and aliases in the Tag manager (#70)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui import tag_manager
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.tag_manager import TagDetails, TagManagerPage
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


def _details(window: MainWindow, session: KeepSession, name: str) -> TagDetails:
    page = _page(window)
    tree = session.tag_cache.get()
    [tag_id] = [s.tag_id for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
    assert page.select_tag(tag_id)
    return page.details


def _node(session: KeepSession, name: str):  # type: ignore[no-untyped-def]
    tree = session.tag_cache.get()
    [tag_id] = [s.tag_id for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
    return tree.node(tag_id), tree.aliases(tag_id)


def _wait_status(qtbot: QtBot, window: MainWindow, expected: str) -> None:
    def check() -> None:
        assert window.statusBar().currentMessage() == expected

    qtbot.waitUntil(check, timeout=5000)


def _aliases(details: TagDetails) -> list[str]:
    return [details.aliases.item(i).text() for i in range(details.aliases.count())]


def test_choosing_and_clearing_a_color(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tag_manager, "pick_color", lambda parent, initial: "#2e86de")
    details = _details(window, session, "Sky")
    assert not details.clear_color.isEnabled()  # no color yet
    details.choose_color.click()
    _wait_status(qtbot, window, "Set the color of 'Sky' to #2e86de.")
    assert _node(session, "Sky")[0].color == "#2e86de"
    qtbot.waitUntil(lambda: "#2e86de" in details.color.text(), timeout=5000)
    assert details.clear_color.isEnabled()

    details.clear_color.click()
    _wait_status(qtbot, window, "Cleared the color of 'Sky'.")
    assert _node(session, "Sky")[0].color is None
    window.undo_action.trigger()
    qtbot.waitUntil(lambda: _node(session, "Sky")[0].color == "#2e86de", timeout=5000)


def test_editing_the_description(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    details = _details(window, session, "Beach")
    assert not details.save_description.isEnabled()
    details.description.setPlainText("Sand, sun, and sunsets")
    assert details.save_description.isEnabled()
    details.revert_description.click()
    assert details.description.toPlainText() == ""
    assert not details.save_description.isEnabled()

    details.description.setPlainText("Sand, sun, and sunsets")
    details.save_description.click()
    _wait_status(qtbot, window, "Saved the description of 'Beach'.")
    assert _node(session, "Beach")[0].description == "Sand, sun, and sunsets"
    qtbot.waitUntil(lambda: not details.save_description.isEnabled(), timeout=5000)


def test_unsaved_text_survives_a_reload(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    details = _details(window, session, "Beach")
    details.description.setPlainText("Half-typed")
    page = _page(window)
    reloads = page.status.text()
    page.reload()  # as after some other tag change
    qtbot.wait(300)
    assert page.status.text() == reloads
    assert details.description.toPlainText() == "Half-typed"
    assert details.save_description.isEnabled()


def test_adding_and_removing_aliases(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    details = _details(window, session, "Iceland")
    assert _aliases(details) == ["Ísland"]
    assert not details.add_alias.isEnabled()
    QTest.keyClicks(details.alias_edit, "Land of Fire and Ice")
    QTest.keyClick(details.alias_edit, Qt.Key.Key_Return)
    _wait_status(qtbot, window, "'Iceland' is now also called 'Land of Fire and Ice'.")
    qtbot.waitUntil(lambda: len(_aliases(details)) == 2, timeout=5000)
    assert set(_node(session, "Iceland")[1]) == {"Ísland", "Land of Fire and Ice"}
    assert details.alias_edit.text() == ""

    # The Tags panel's filter finds it by its new name.
    window.tag_panel.filter_edit.setText("fire")
    assert window.tag_panel.model.matches() == {_node(session, "Iceland")[0].id}

    details.aliases.setCurrentRow(_aliases(details).index("Land of Fire and Ice"))
    details.remove_alias.click()
    _wait_status(qtbot, window, "'Iceland' is no longer called 'Land of Fire and Ice'.")
    assert _node(session, "Iceland")[1] == ("Ísland",)


def test_an_alias_like_the_name_is_refused(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    details = _details(window, session, "Iceland")
    details.alias_edit.setText("iceland")
    details.add_alias.click()
    qtbot.waitUntil(
        lambda: "differ from the tag's name" in window.statusBar().currentMessage(), timeout=5000
    )
    assert _node(session, "Iceland")[1] == ("Ísland",)


def test_a_draft_survives_looking_at_another_tag(window: MainWindow, session: KeepSession) -> None:
    details = _details(window, session, "Beach")
    details.description.setPlainText("Half-typed")
    _details(window, session, "Iceland")
    assert details.description.toPlainText() == "Summer 2019 road trip around the Ring Road"
    _details(window, session, "Beach")
    assert details.description.toPlainText() == "Half-typed"
    details.revert_description.click()  # Revert drops the draft
    _details(window, session, "Iceland")
    _details(window, session, "Beach")
    assert details.description.toPlainText() == ""
