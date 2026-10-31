"""Type to find in the results (#633): the typed letters go to the next title starting with
them, over every result (rows not loaded yet too), with a pause between keys of up to a
second."""

import importlib.util
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView
from pytestqt.qtbot import QtBot

from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui import type_ahead
from tagalot.ui.models.results import ResultColumn, ResultsModel
from tagalot.ui.result_grid import ResultGrid
from tagalot.ui.result_table import ResultTable
from tagalot.ui.result_tree import ResultTree, ResultTreeModel, _top_row

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"

TITLES = [
    "Family at the beach.jpg",
    "geyser.jpg",
    "glacier.jpg",
    "März.txt",
    "northern lights.png",
    "notes.txt",
    "readme.md",
    "sunset.jpg",
    "tax 2025.pdf",
]


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keep: Path = module.make_demo(tmp_path)
    with KeepSession.open(keep, Settings()) as session:
        yield session


def _grid(model: ResultsModel, session: KeepSession) -> QAbstractItemView:
    view = ResultGrid()
    view.setModel(model)
    return view


def _table(model: ResultsModel, session: KeepSession) -> QAbstractItemView:
    view = ResultTable()
    view.setModel(model)
    return view


def _tree(model: ResultsModel, session: KeepSession) -> QAbstractItemView:
    view = ResultTree()
    view.setModel(ResultTreeModel(model, session, pool=QThreadPool.globalInstance(), parent=view))
    return view


VIEWS = [_grid, _table, _tree]
Build = Callable[[ResultsModel, KeepSession], QAbstractItemView]


def _view(qtbot: QtBot, session: KeepSession, build: Build) -> QAbstractItemView:
    """A view of the demo's 9 items, 2 rows a page: only the first page loads at first."""
    model = ResultsModel(session, page_size=2)
    view = build(model, session)
    model.setParent(view)
    qtbot.addWidget(view)
    model.set_search(SearchSpec(), [ResultColumn("title", "Title")])
    qtbot.waitUntil(lambda: model.total == len(TITLES), timeout=5000)
    assert [model.hit(0).title, model.hit(1).title] == TITLES[:2]  # type: ignore[union-attr]
    assert model.hit(5) is None
    return view


def _current(view: QAbstractItemView) -> int:
    index = view.currentIndex()
    return _top_row(index) if index.isValid() else -1


def _wait_for(qtbot: QtBot, view: QAbstractItemView, title: str) -> None:
    def there() -> bool:
        return _current(view) == TITLES.index(title)

    try:
        qtbot.waitUntil(there, timeout=5000)
    except AssertionError:
        row = _current(view)
        seen = TITLES[row] if row >= 0 else None
        raise AssertionError(f"wanted {title!r}, at {seen!r}") from None


@pytest.mark.parametrize("build", VIEWS)
def test_the_typed_letters_find_rows_not_loaded_yet(
    qtbot: QtBot, session: KeepSession, build: Build
) -> None:
    view = _view(qtbot, session, build)
    QTest.keyClicks(view, "not")
    _wait_for(qtbot, view, "notes.txt")
    QTest.keyClicks(view, "")  # nothing typed: nothing moves
    assert _current(view) == TITLES.index("notes.txt")


def test_keys_at_typing_speed_add_up(qtbot: QtBot, session: KeepSession) -> None:
    """Qt's own pause, 400 ms, is shorter than ordinary typing."""
    view = _view(qtbot, session, _grid)
    QTest.keyClicks(view, "g")
    _wait_for(qtbot, view, "geyser.jpg")
    QTest.qWait(500)
    QTest.keyClicks(view, "l")
    _wait_for(qtbot, view, "glacier.jpg")


def test_a_pause_starts_again(
    qtbot: QtBot, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(type_ahead, "TYPING_PAUSE_MS", 50)
    view = _view(qtbot, session, _grid)
    QTest.keyClicks(view, "g")
    _wait_for(qtbot, view, "geyser.jpg")
    QTest.qWait(150)
    QTest.keyClicks(view, "s")
    _wait_for(qtbot, view, "sunset.jpg")
    QTest.qWait(150)
    QTest.keyClicks(view, "g")  # a new search starts after the current item, and wraps
    _wait_for(qtbot, view, "geyser.jpg")


def test_one_letter_repeated_steps_through(qtbot: QtBot, session: KeepSession) -> None:
    view = _view(qtbot, session, _table)
    QTest.keyClicks(view, "n")
    _wait_for(qtbot, view, "northern lights.png")
    QTest.keyClicks(view, "n")
    _wait_for(qtbot, view, "notes.txt")
    QTest.keyClicks(view, "N")  # case doesn't matter; wraps around
    _wait_for(qtbot, view, "northern lights.png")


def test_more_letters_keep_a_current_item_that_matches(qtbot: QtBot, session: KeepSession) -> None:
    view = _view(qtbot, session, _grid)
    QTest.keyClicks(view, "no")
    _wait_for(qtbot, view, "northern lights.png")
    QTest.keyClicks(view, "t")
    _wait_for(qtbot, view, "notes.txt")


def test_a_new_search_forgets_what_was_typed(qtbot: QtBot, session: KeepSession) -> None:
    view = _view(qtbot, session, _grid)
    model = view.model()
    assert isinstance(model, ResultsModel)
    QTest.keyClicks(view, "s")
    _wait_for(qtbot, view, "sunset.jpg")
    model.set_search(SearchSpec(), [ResultColumn("title", "Title")])
    qtbot.waitUntil(lambda: model.total == len(TITLES), timeout=5000)
    QTest.keyClicks(view, "t")  # "t", not "st"
    _wait_for(qtbot, view, "tax 2025.pdf")
