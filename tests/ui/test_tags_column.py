"""Tests for the optional Tags column in result lists (#197)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from pytestqt.qtbot import QtBot

from tagalot.core.search import SearchHit
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import PATH_SEPARATOR, TagNode, TagTree, entity_tags
from tagalot.core.ui_state import load_ui_state
from tagalot.ui.grouped_results import GroupedResults, TypeGroup
from tagalot.ui.main_window import MainWindow
from tagalot.ui.models.results import TAGS, ResultColumn, TagsValue, tags_value
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


def _window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()
    qtbot.waitUntil(lambda: _page(window).status.text() == "9 items", timeout=5000)
    return window


def _page(window: MainWindow) -> SearchPage:
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    return page


def _tags_column(page: SearchPage) -> int:
    return [c.key for c in page.model.columns].index(TAGS)


def _cell(page: SearchPage, title: str, role: int = Qt.ItemDataRole.DisplayRole) -> str:
    column = _tags_column(page)
    for row in range(page.model.rowCount()):
        if page.model.index(row, 0).data() == title:
            value: str = page.model.index(row, column).data(role)
            return value
    raise AssertionError(title)


def test_entity_tags(session: KeepSession) -> None:
    with session.reader.connect() as conn:
        tagged = entity_tags(conn, range(1, 1000))
    assert len(tagged) == 7  # notes.txt and readme.md have no tags
    assert sorted(len(t) for t in tagged.values()) == [1, 1, 1, 2, 2, 2, 3]


def test_tags_value_names_sorted_with_paths_for_ambiguous_names() -> None:
    tree = TagTree(
        [
            TagNode(1, None, "Places", None, 0),
            TagNode(2, 1, "Beach", None, 0),
            TagNode(3, None, "Moods", None, 1),
            TagNode(4, 3, "Beach", None, 0),  # same name, other parent
            TagNode(5, None, "Favorites", None, 2),
        ],
        {},
    )
    value = tags_value(tree, [5, 2, 99])  # 99: a deleted tag, skipped
    assert value == TagsValue(
        f"Favorites, Places{PATH_SEPARATOR}Beach",
        f"Favorites\nPlaces{PATH_SEPARATOR}Beach",
    )
    assert tags_value(tree, []) == TagsValue("", "")


def test_the_tags_column_is_hidden_until_shown_and_remembered(
    qtbot: QtBot, session: KeepSession
) -> None:
    window = _window(qtbot, session)
    page = _page(window)
    column = _tags_column(page)
    assert page.table.isColumnHidden(column)

    menu = page.table.column_menu()
    actions = {a.text(): a for a in menu.actions()}
    assert not actions["Name"].isEnabled()  # the title always shows
    assert not actions["Tags"].isChecked()
    actions["Tags"].setChecked(True)  # as clicking it in the header's menu does
    assert not page.table.isColumnHidden(column)
    qtbot.waitUntil(lambda: _cell(page, "Family at the beach.jpg") != "", timeout=5000)
    assert _cell(page, "Family at the beach.jpg") == "Beach, Family, Favorites"
    tooltip = _cell(page, "Family at the beach.jpg", Qt.ItemDataRole.ToolTipRole)
    assert f"Places{PATH_SEPARATOR}Beach" in tooltip.split("\n")
    assert _cell(page, "notes.txt") == ""
    state = load_ui_state(session.keep.ui_state_path)
    assert state["hidden_columns"] == {"search:": []}

    again = _window(qtbot, session)  # a new window for the keep restores the choice
    assert not _page(again).table.isColumnHidden(_tags_column(_page(again)))
    actions = {a.text(): a for a in _page(again).table.column_menu().actions()}
    actions["Size (bytes)"].setChecked(False)
    assert _page(again).table.isColumnHidden(4)  # title, tags, extension, folder, size
    assert load_ui_state(session.keep.ui_state_path)["hidden_columns"] == {"search:": ["size"]}


def test_the_tags_column_updates_after_tagging(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    page = _page(window)
    {a.text(): a for a in page.table.column_menu().actions()}["Tags"].setChecked(True)
    notes = next(
        h
        for r in range(page.model.rowCount())
        if (h := page.model.hit(r)) and h.title == "notes.txt"
    )
    tree = session.tag_cache.get()
    sky = next(s.tag_id for s in tree.suggest("Sky"))
    window.apply_tags([notes.id], [sky])
    qtbot.waitUntil(lambda: _cell(page, "notes.txt") == "Sky", timeout=5000)


def test_sections_hide_the_same_columns(qtbot: QtBot) -> None:
    columns = (ResultColumn("title", "Title"), ResultColumn(TAGS, "Tags", sortable=False))
    hit = SearchHit(1, "t", "A")
    group = TypeGroup("t", "Things", 1, columns, ((hit, {TAGS: TagsValue("X", "X")}),))
    groups = GroupedResults()
    qtbot.addWidget(groups)
    groups.set_hidden_columns({TAGS})
    groups.set_groups([group, group])
    assert all(s.table.isColumnHidden(1) for s in groups.sections)
    groups.set_hidden_columns(set())
    assert not any(s.table.isColumnHidden(1) for s in groups.sections)
    with qtbot.waitSignal(groups.column_toggled) as blocker:
        {a.text(): a for a in groups.sections[1].table.column_menu().actions()}["Tags"].setChecked(
            False
        )
    assert blocker.args == [TAGS, False]
