"""Tests for the Tag manager page: the tree with usage counts, and the details pane (#67)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QModelIndex, Qt, QThreadPool
from PySide6.QtTest import QAbstractItemModelTester
from pytestqt.qtbot import QtBot

from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import PATH_SEPARATOR, TagNode, TagTree, TagUsage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.tag_manager import ITEMS, WITH_SUBTAGS, TagManagerModel, TagManagerPage
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


def test_the_model_has_count_columns(qapp: Any) -> None:
    model = TagManagerModel()
    tester = QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Fatal)
    tree = TagTree([TagNode(1, None, "Places", None, 0), TagNode(2, 1, "Iceland", None, 0)], {})
    model.set_tree(tree)
    model.set_usage({1: TagUsage(0, 1234), 2: TagUsage(3, 3)})
    assert model.columnCount() == 3
    assert [model.headerData(c, Qt.Orientation.Horizontal) for c in range(3)] == [
        "Tag",
        "Items",
        "With sub-tags",
    ]
    places = model.index(0, 0)
    assert model.index(0, WITH_SUBTAGS).data() == "1,234"
    assert model.index(0, ITEMS, QModelIndex()).data() == "0"
    assert model.index(0, ITEMS, places).data() == "3"  # Iceland, nested
    assert model.parent(model.index(0, WITH_SUBTAGS, places)) == places
    del tester


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
    qtbot.waitUntil(lambda: _manager(window).loaded, timeout=5000)
    return window


def _manager(window: MainWindow) -> TagManagerPage:
    page = window.stack.currentWidget()
    assert isinstance(page, TagManagerPage)
    return page


def _counts(page: TagManagerPage, name: str) -> tuple[str, str]:
    tree = page.model.tree
    assert tree is not None
    [tag_id] = [s.tag_id for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
    return (
        page.model.index_at(tag_id, ITEMS).data(),
        page.model.index_at(tag_id, WITH_SUBTAGS).data(),
    )


def _select(page: TagManagerPage, name: str) -> None:
    tree = page.model.tree
    assert tree is not None
    [tag_id] = [s.tag_id for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
    assert page.select_tag(tag_id)


def test_the_page_shows_every_tag_with_counts(window: MainWindow) -> None:
    page = _manager(window)
    assert page.status.text() == "9 tags"
    assert _counts(page, "Places") == ("0", "5")  # Iceland's 3 and Beach's 2
    assert _counts(page, "Iceland") == ("3", "3")
    assert _counts(page, "Favorites") == ("2", "2")
    assert _counts(page, "Topics") == ("0", "4")  # Money 2, Sky 2 (no overlap)


def test_the_details_pane(window: MainWindow) -> None:
    page = _manager(window)
    details = page.details
    assert details.empty.isVisible()
    _select(page, "Iceland")
    assert details.title.text() == "Iceland"
    assert details.path.text() == f"Places{PATH_SEPARATOR}Iceland"
    assert details.description.text() == "Summer 2019 road trip around the Ring Road"
    assert details.aliases.text() == "Ísland"
    assert details.color.text() == "None"
    assert details.counts.text() == "3 items directly; 3 with its sub-tags"
    _select(page, "Beach")
    assert details.description.text() == "None"


def test_filtering(window: MainWindow) -> None:
    page = _manager(window)
    page.filter_edit.setText("ice")
    assert page.model.rowCount() == 1  # Places, holding Iceland
    assert page.view.isExpanded(page.model.index(0, 0))
    page.filter_edit.clear()
    assert page.model.rowCount() == 4


def test_counts_follow_tagging(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _manager(window)
    tree = session.tag_cache.get()
    [favorites] = [
        s.tag_id for s in tree.suggest("Favorites") if tree.node(s.tag_id).name == "Favorites"
    ]
    with session.reader.connect() as conn:
        notes = next(h for h in run_search(conn, SearchSpec(text="notes"), tree))
    window.apply_tags([notes.id], [favorites])
    qtbot.waitUntil(lambda: _counts(page, "Favorites") == ("3", "3"), timeout=5000)
