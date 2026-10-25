"""The Tags panel's tag groups (#535): Groups ▾ chooses which groups show, each as a heading
over its tags; while filtering, matches outside them follow under Other tags; right-click a
tag → Groups puts it in a group or takes it out; the choice is the user's, kept in
ui_state.json."""

from collections.abc import Iterator
from typing import Any

import pytest
from PySide6.QtCore import QModelIndex, Qt, QThreadPool
from PySide6.QtTest import QAbstractItemModelTester
from PySide6.QtWidgets import QInputDialog, QMenu
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.tags import GroupNode, TagNode, TagTree
from tagalot.core.ui_state import load_ui_state
from tagalot.ui.main_window import MainWindow
from tagalot.ui.models.tag_tree import OTHER_TAGS, TagTreeModel
from tagalot.ui.tag_panel import TagPanel
from tagalot.ui.workers import ScanController
from tests.ui.test_tag_panel import _rows, _shown, _tag, session

pytestmark = pytest.mark.gui

__all__ = ["session"]  # a fixture

GENRE, ROCK, PUNK, JAZZ, MOOD, CALM, MOOD_ROCK = range(1, 8)
MUSIC, MIXED = 1, 2
TREE = TagTree(
    [
        TagNode(GENRE, None, "Genre", None, 0),
        TagNode(ROCK, GENRE, "Rock", None, 0),
        TagNode(PUNK, ROCK, "Punk", None, 0),
        TagNode(JAZZ, GENRE, "Jazz", None, 0),
        TagNode(MOOD, None, "Mood", None, 0),
        TagNode(CALM, MOOD, "Calm", None, 0),
        TagNode(MOOD_ROCK, MOOD, "Rock", None, 0),
    ],
    {},
    groups=[
        GroupNode(MUSIC, "Music", 0, frozenset({ROCK})),
        GroupNode(MIXED, "Mixed", 1, frozenset({CALM, ROCK, PUNK})),
    ],
)


@pytest.fixture
def model(qapp: Any) -> Iterator[TagTreeModel]:
    model = TagTreeModel()
    tester = QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Fatal)
    model.set_tree(TREE)
    yield model
    del tester


def test_groups_are_headings_over_their_tags(model: TagTreeModel) -> None:
    model.set_groups([MUSIC, MIXED])
    assert _rows(model) == [
        "Music",
        "  Rock",
        "    Punk",
        "Mixed",  # Punk is under Rock, so it's listed there, not again on its own
        "  Rock",
        "    Punk",
        "  Calm",
    ]
    assert model.visible_count() == 5  # tag rows: Rock and Punk twice
    music = model.index(0, 0)
    assert model.tag_id(music) is None
    assert model.heading_key(music) == "group:1"
    assert model.flags(music) == Qt.ItemFlag.ItemIsEnabled  # not selected, dragged, or ticked
    assert model.headings() == [music, model.index(1, 0)]
    assert [model.parent(i) for i in model.indexes_of(ROCK)] == [music, model.index(1, 0)]
    assert model.index_of(ROCK) == model.index(0, 0, music)
    assert model.data(music, Qt.ItemDataRole.ToolTipRole).startswith("The tag group")

    model.set_groups([MIXED])
    assert _rows(model)[0] == "Mixed"
    model.set_groups([99])  # a group that's gone: every tag
    assert _rows(model)[0] == "Genre"
    assert model.shown_groups() is None
    model.set_groups(None)
    assert model.visible_count() == 7


def test_filtering_puts_other_matches_under_other_tags(model: TagTreeModel) -> None:
    model.set_groups([MUSIC, MIXED])
    model.set_filter("rock")
    assert _rows(model) == [
        "Music",
        "  Rock",
        "Mixed",
        "  Rock",
        OTHER_TAGS,
        "  Mood",
        "    Rock",
    ]
    other = model.index(2, 0)
    assert model.heading_key(other) == "other"
    assert model.data(model.first_match(), Qt.ItemDataRole.DisplayRole) == "Rock"
    assert model.parent(model.first_match()) == model.index(0, 0)
    model.set_filter("jazz")  # nothing in the groups matches: only Other tags
    assert _rows(model) == [OTHER_TAGS, "  Genre", "    Jazz"]


def _menu(menu: QMenu, text: str) -> Any:
    [action] = [a for a in menu.actions() if a.text() == text]
    return action


@pytest.fixture
def panel(qtbot: QtBot, session: KeepSession) -> TagPanel:
    tree = session.tag_cache.get()
    places = next(t for t in tree.children(None) if tree.node(t).name == "Places")
    session.tags.add_group("Trips", [places])
    session.tags.add_group("Everyday", [])
    panel = TagPanel(session, pool=QThreadPool())
    qtbot.addWidget(panel)
    panel.resize(260, 500)
    panel.show()
    qtbot.waitUntil(lambda: panel.loaded, timeout=5000)
    return panel


def test_groups_button_chooses_what_shows(qtbot: QtBot, panel: TagPanel) -> None:
    assert panel.groups_button.text() == "All tags"
    chosen: list[object] = []
    panel.groups_changed.connect(chosen.append)
    panel._fill_groups_menu()
    texts = [a.text() for a in panel.groups_menu.actions() if not a.isSeparator()]
    assert texts == ["All tags", "Trips", "Everyday"]
    assert _menu(panel.groups_menu, "All tags").isChecked()
    trips = panel.session.tag_cache.get().find_group("Trips")
    _menu(panel.groups_menu, "Trips").setChecked(True)
    assert chosen == [[trips]]
    assert panel.groups_button.text() == "Trips"
    assert _shown(panel) == ["Trips", "  🌍 Places", "    Iceland", "    Beach"]

    view = panel.view
    view.collapse(panel.model.headings()[0])  # the user's fold of a heading is kept
    panel.reload()
    qtbot.wait(200)
    assert _shown(panel) == ["Trips"]
    panel.expand_button.click()
    panel.collapse_button.click()  # collapsing all keeps headings open, so tags show
    assert _shown(panel) == ["Trips", "  🌍 Places"]

    panel._fill_groups_menu()
    _menu(panel.groups_menu, "Everyday").setChecked(True)
    assert panel.groups_button.text() == "2 groups"
    assert _shown(panel)[-1] == "Everyday"  # an empty group still shows its heading
    panel.filter_edit.setText("sky")
    assert _shown(panel) == [OTHER_TAGS, "  Topics", "    Sky"]
    panel.filter_edit.clear()

    heading = panel.menu_for(panel.model.headings()[0])
    assert heading is not None
    _menu(heading, "Stop showing “Trips”").trigger()
    assert panel.groups_button.text() == "Everyday"
    panel._fill_groups_menu()
    _menu(panel.groups_menu, "All tags").trigger()
    assert chosen[-1] is None
    assert _shown(panel)[0] == "🌍 Places"


def test_right_click_puts_tags_in_groups(
    qtbot: QtBot, panel: TagPanel, monkeypatch: pytest.MonkeyPatch
) -> None:
    sky = _tag(panel, "Sky")
    menu = panel.menu_for(panel.model.index_of(sky))
    assert menu is not None
    [groups] = [a.menu() for a in menu.actions() if a.menu() is not None]
    assert isinstance(groups, QMenu)
    texts = [a.text() for a in groups.actions() if not a.isSeparator()]
    assert texts == ["Trips", "Everyday", "New group…"]
    assert not _menu(groups, "Trips").isChecked()
    asked: list[tuple[int, list[int], bool]] = []
    panel.group_requested.connect(lambda g, tags, on: asked.append((g, tags, on)))
    _menu(groups, "Everyday").trigger()
    everyday = panel.session.tag_cache.get().find_group("Everyday")
    assert asked == [(everyday, [sky], True)]

    made: list[tuple[str, list[int]]] = []
    panel.new_group_requested.connect(lambda name, tags: made.append((name, tags)))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Weather", True))
    _menu(groups, "New group…").trigger()
    assert made == [("Weather", [sky])]


def test_the_window_does_it_and_remembers_the_choice(
    qtbot: QtBot, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.show()
    panel = window.tag_panel
    qtbot.waitUntil(lambda: panel.loaded, timeout=5000)
    sky = _tag(panel, "Sky")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Weather", True))
    panel.ask_new_group([sky])

    def made() -> None:
        assert window.statusBar().currentMessage() == "Made the group 'Weather' with 'Sky'."

    qtbot.waitUntil(made, timeout=5000)
    weather = session.tag_cache.get().find_group("Weather")
    assert weather is not None
    qtbot.waitUntil(
        lambda: panel.model.tree is not None and bool(panel.model.tree.groups()), timeout=5000
    )
    panel.set_groups([weather], by_user=True)
    assert load_ui_state(session.keep.ui_state_path)["tag_groups"] == [weather]
    assert _shown(panel) == ["Weather", "  Sky"]

    beach = _tag(panel, "Beach")
    window._set_in_group(weather, [beach], True)

    def added() -> None:
        assert _shown(panel) == ["Weather", "  Beach", "  Sky"]

    qtbot.waitUntil(added, timeout=5000)
    assert window.statusBar().currentMessage() == "Added 'Beach' to the group 'Weather'."
    window.undo_action.trigger()
    qtbot.waitUntil(lambda: _shown(panel) == ["Weather", "  Sky"], timeout=5000)
    window.close()

    again = MainWindow(session, scans=ScanController(QThreadPool()))  # opened again
    qtbot.addWidget(again)
    qtbot.waitUntil(lambda: again.tag_panel.loaded, timeout=5000)
    assert again.tag_panel.groups_button.text() == "Weather"
    assert again.tag_panel.model.index(0, 0, QModelIndex()).data() == "Weather"
    again.tag_panel.set_groups(None, by_user=True)
    assert "tag_groups" not in load_ui_state(session.keep.ui_state_path)
    again.close()
