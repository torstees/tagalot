"""Only their tags (#541): the Tags panel's toggle that shows only the tags the selected items
have, with their parents; with groups shown, the rest follow under Other tags, greyed."""

from collections.abc import Iterator
from typing import Any

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtTest import QAbstractItemModelTester
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.ui.main_window import MainWindow
from tagalot.ui.models.tag_tree import OTHER_TAGS, TagTreeModel
from tests.ui.test_keyboard_tagging import _select, session, window
from tests.ui.test_tag_groups_panel import CALM, MUSIC, PUNK, TREE
from tests.ui.test_tag_panel import _rows, _shown

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


@pytest.fixture
def model(qapp: Any) -> Iterator[TagTreeModel]:
    model = TagTreeModel()
    tester = QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Fatal)
    model.set_tree(TREE)
    yield model
    del tester


def _dim(model: TagTreeModel, parent: QModelIndex | None = None) -> list[str]:
    """The greyed rows' names."""
    parent = parent or QModelIndex()
    found = []
    for row in range(model.rowCount(parent)):
        index = model.index(row, 0, parent)
        if model.is_dim(index):
            found.append(index.data())
        found += _dim(model, index)
    return found


def test_only_the_selected_items_tags_with_their_parents(model: TagTreeModel) -> None:
    model.set_applied_only(True)
    assert _rows(model) == []  # nothing selected
    model.set_selection(2, {PUNK: 2, CALM: 1})
    assert _rows(model) == ["Genre", "  Rock", "    Punk", "Mood", "  Calm"]
    assert model.check_state(PUNK) == Qt.CheckState.Checked
    assert model.check_state(CALM) == Qt.CheckState.PartiallyChecked
    assert _dim(model) == []

    model.set_groups([MUSIC])  # Rock: its Punk under the heading, Calm greyed under Other
    assert _rows(model) == ["Music", "  Rock", "    Punk", OTHER_TAGS, "  Mood", "    Calm"]
    assert _dim(model) == ["Mood", "Calm"]
    model.set_filter("calm")
    assert _rows(model) == [OTHER_TAGS, "  Mood", "    Calm"]
    model.set_filter("")

    model.set_selection(1, {})  # a selected item with no tags
    assert _rows(model) == []
    model.set_applied_only(False)
    assert _rows(model)[0] == "Music"
    assert _dim(model) == []


def test_the_button_follows_the_selection(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    panel = window.tag_panel
    panel.applied_button.click()
    assert panel.message.text() == "Select items to see the tags they have."
    _select(window, "sunset.jpg")
    qtbot.waitUntil(lambda: panel.model.selected_count == 1, timeout=5000)
    assert _shown(panel) == ["🌍 Places", "  Beach", "Topics", "  Sky"]

    _select(window, "glacier.jpg")  # now both
    qtbot.waitUntil(lambda: panel.model.selected_count == 2, timeout=5000)
    assert _shown(panel) == [
        "🌍 Places",
        "  Iceland",
        "  Beach",
        "Topics",
        "  Sky",
        "⭐ Favorites",
    ]

    tree = session.tag_cache.get()
    places = next(t for t in tree.children(None) if tree.node(t).name == "Places")
    session.tags.add_group("Trips", [places])
    panel.reload()
    qtbot.waitUntil(lambda: bool(panel.model.tree and panel.model.tree.groups()), timeout=5000)
    trips = session.tag_cache.get().find_group("Trips")
    assert trips is not None
    panel.set_groups([trips])
    assert _shown(panel) == [
        "Trips",
        "  🌍 Places",
        "    Iceland",
        "    Beach",
        OTHER_TAGS,
        "  Topics",
        "    Sky",
        "  ⭐ Favorites",
    ]

    panel.applied_button.click()  # every tag again, in the group
    assert _shown(panel) == ["Trips", "  🌍 Places", "    Iceland", "    Beach"]
