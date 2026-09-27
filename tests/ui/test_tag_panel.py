"""Tests for the tag tree model and the tagging panel's filter box."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QModelIndex, Qt, QThreadPool
from PySide6.QtGui import QFont, QIcon
from PySide6.QtTest import QAbstractItemModelTester, QTest
from pytestqt.qtbot import QtBot

from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import PATH_SEPARATOR, TagNode, TagTree
from tagalot.ui.models.tag_tree import TAG_ID_ROLE, TagTreeModel
from tagalot.ui.tag_panel import TagPanel

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"

GENRE, ROCK, PUNK, JAZZ, MOOD, CALM, MOOD_ROCK, XMAS = range(1, 9)
# id, parent, name, color
TAGS = [
    (GENRE, None, "Genre", None),
    (ROCK, GENRE, "Rock", "#d33"),
    (PUNK, ROCK, "Punk", None),
    (JAZZ, GENRE, "Jazz", "not a color"),
    (MOOD, None, "Mood", None),
    (CALM, MOOD, "Calm", None),
    (MOOD_ROCK, MOOD, "Rock", None),  # same name, other parent
    (XMAS, None, "Christmas", None),
]
TREE = TagTree(
    [TagNode(i, p, n, c, 0) for i, p, n, c in TAGS], {PUNK: ["hardcore"], CALM: ["chill"]}
)


def _rows(model: TagTreeModel, parent: QModelIndex | None = None, depth: int = 0) -> list[str]:
    """The shown tree as indented names."""
    parent = parent or QModelIndex()
    lines = []
    for row in range(model.rowCount(parent)):
        index = model.index(row, 0, parent)
        lines.append("  " * depth + index.data())
        lines.extend(_rows(model, index, depth + 1))
    return lines


@pytest.fixture
def model(qapp: Any) -> Iterator[TagTreeModel]:
    """The model under Qt's model tester, which checks parents, rows, and indexes after every
    change. The tester goes before the model: pytest-qt's ``qtmodeltester`` fixture outlives
    it, which crashed PySide6 (6.11) once the model had returned an icon."""
    model = TagTreeModel()
    tester = QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Fatal)
    model.set_tree(TREE)
    yield model
    del tester


def test_the_whole_tree(model: TagTreeModel) -> None:
    assert _rows(model) == [  # same sort order, so by name
        "Christmas",
        "Genre",
        "  Jazz",
        "  Rock",
        "    Punk",
        "Mood",
        "  Calm",
        "  Rock",
    ]
    assert not model.filtering
    assert model.visible_count() == len(TAGS)


def test_filtering_keeps_ancestors_and_matches_aliases(model: TagTreeModel) -> None:
    model.set_filter("rock")
    assert _rows(model) == ["Genre", "  Rock", "Mood", "  Rock"]
    assert model.matches() == {ROCK, MOOD_ROCK}
    model.set_filter("CHILL")  # an alias of Calm, shown so the match makes sense
    assert _rows(model) == ["Mood", "  Calm (chill)"]
    model.set_filter("hard")  # an alias, two levels down
    assert _rows(model) == ["Genre", "  Rock", "    Punk (hardcore)"]
    model.set_filter("pun")  # the name matches: no alias shown
    assert _rows(model) == ["Genre", "  Rock", "    Punk"]
    model.set_filter("zebra")
    assert _rows(model) == []
    model.set_filter("  ")  # blank: everything again
    assert model.visible_count() == len(TAGS)


def test_matches_are_bold_and_context_is_not(model: TagTreeModel) -> None:
    model.set_filter("punk")
    genre = model.index_of(GENRE)
    punk = model.index_of(PUNK)
    font = punk.data(Qt.ItemDataRole.FontRole)
    assert isinstance(font, QFont)
    assert font.bold()
    assert genre.data(Qt.ItemDataRole.FontRole) is None
    assert model.first_match() == punk


def test_indexes_parents_and_roles(model: TagTreeModel) -> None:
    punk = model.index_of(PUNK)
    assert punk.data(TAG_ID_ROLE) == PUNK
    assert model.parent(punk) == model.index_of(ROCK)
    assert model.parent(model.index_of(GENRE)) == QModelIndex()
    tooltip = punk.data(Qt.ItemDataRole.ToolTipRole)
    assert tooltip == f"Genre{PATH_SEPARATOR}Rock{PATH_SEPARATOR}Punk\nAlso: hardcore"
    assert isinstance(model.index_of(ROCK).data(Qt.ItemDataRole.DecorationRole), QIcon)
    assert model.index_of(JAZZ).data(Qt.ItemDataRole.DecorationRole) is None  # bad color
    assert model.index_of(999) == QModelIndex()


# --- the panel ---


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
def panel(qtbot: QtBot, session: KeepSession) -> TagPanel:
    panel = TagPanel(session, pool=QThreadPool())
    qtbot.addWidget(panel)
    panel.resize(260, 500)
    panel.show()
    qtbot.waitUntil(lambda: panel.loaded, timeout=5000)
    return panel


def _shown(panel: TagPanel) -> list[str]:
    """Names of the rows the view shows, following its folds."""
    lines = []

    def walk(parent: QModelIndex, depth: int) -> None:
        for row in range(panel.model.rowCount(parent)):
            index = panel.model.index(row, 0, parent)
            lines.append("  " * depth + index.data())
            if panel.view.isExpanded(index):
                walk(index, depth + 1)

    walk(QModelIndex(), 0)
    return lines


def _tag(panel: TagPanel, name: str) -> int:
    tree = panel.model.tree
    assert tree is not None
    [tag_id] = [s.tag_id for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
    return tag_id


def test_the_panel_shows_the_keeps_tags_top_level_open(panel: TagPanel) -> None:
    assert _shown(panel) == [  # in the order the demo created them
        "Places",
        "  Iceland",
        "  Beach",
        "People",
        "  Family",
        "Topics",
        "  Money",
        "  Sky",
        "Favorites",
    ]
    assert not panel.message.isVisible()


def test_filtering_opens_matches_and_clearing_restores_folds(qtbot: QtBot, panel: TagPanel) -> None:
    panel.view.collapse(panel.model.index_of(_tag(panel, "Places")))
    QTest.keyClicks(panel.filter_edit, "ice")
    assert _shown(panel) == ["Places", "  Iceland"]
    QTest.keyClick(panel.filter_edit, Qt.Key.Key_Escape)  # clears the filter
    assert panel.filter_edit.text() == ""
    assert "  Iceland" not in _shown(panel)  # Places is folded again, as the user left it
    assert "  Family" in _shown(panel)


def test_filtering_by_alias_and_no_matches(qtbot: QtBot, panel: TagPanel) -> None:
    QTest.keyClicks(panel.filter_edit, "finance")
    assert _shown(panel) == ["Topics", "  Money (finance)"]
    panel.filter_edit.setText("zebra")
    assert _shown(panel) == []
    assert panel.message.isVisible()
    assert panel.message.text() == "No tags match “zebra”."


def test_down_moves_to_the_first_match(qtbot: QtBot, panel: TagPanel) -> None:
    panel.filter_edit.setFocus()
    QTest.keyClicks(panel.filter_edit, "sky")
    QTest.keyClick(panel.filter_edit, Qt.Key.Key_Down)
    assert panel.view.hasFocus()
    assert panel.current_tag() == _tag(panel, "Sky")


def test_reload_keeps_folds_filter_and_highlight(
    qtbot: QtBot, panel: TagPanel, session: KeepSession
) -> None:
    panel.view.collapse(panel.model.index_of(_tag(panel, "People")))
    assert panel.select_tag(_tag(panel, "Beach"))
    session.tags.add(_tag(panel, "Places"), "Norway")
    panel.reload()
    qtbot.waitUntil(lambda: "  Norway" in _shown(panel), timeout=5000)
    assert "  Family" not in _shown(panel)  # People stays folded
    assert panel.current_tag() == _tag(panel, "Beach")


def test_an_empty_keep(qtbot: QtBot, tmp_path: Path) -> None:
    keep = create_keep(tmp_path / "Empty.keep", "Empty", ThemeRef("generic", 1))
    with KeepSession.open(keep.dir, Settings()) as session:
        panel = TagPanel(session, pool=QThreadPool())
        qtbot.addWidget(panel)
        panel.show()
        qtbot.waitUntil(lambda: panel.loaded, timeout=5000)
        assert panel.message.text() == "No tags yet."
        assert panel.model.rowCount() == 0
