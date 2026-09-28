"""Tests for tagging by dragging tags from the panel onto results (DESIGN.md §12)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, QMimeData, QPoint, QPointF, Qt, QThreadPool
from PySide6.QtGui import QDragLeaveEvent, QDragMoveEvent, QDropEvent
from pytestqt.qtbot import QtBot

from tagalot.core.search import SearchHit, run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.dnd import TAG_MIME, dragged_tags, tags_mime
from tagalot.ui.grouped_results import TypeGroup, TypeSection
from tagalot.ui.main_window import MainWindow
from tagalot.ui.models.results import ResultColumn, ResultsModel
from tagalot.ui.result_table import ResultTable
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


def _drop(
    table: ResultTable, row: int, tag_ids: list[int] | None, mime: QMimeData | None = None
) -> None:
    """Drop ``tag_ids`` (or ``mime``) on the middle of ``row``, as Qt would deliver it."""
    mime = mime if mime is not None else tags_mime(tag_ids or [])
    y = table.rowViewportPosition(row) + table.rowHeight(row) // 2
    event = QDropEvent(
        QPointF(20, y),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    table.dropEvent(event)


def _select(table: ResultTable, rows: list[int]) -> None:
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    for row in rows:
        table.selectionModel().select(table.model().index(row, 0), flags)


def test_the_drag_payload(qapp: object) -> None:
    assert dragged_tags(tags_mime([3, 1, 3])) == [3, 1]
    garbage = QMimeData()
    garbage.setData(TAG_MIME, b"not json")
    assert dragged_tags(garbage) == []
    assert dragged_tags(QMimeData()) == []
    assert dragged_tags(None) == []


def test_the_panel_drags_the_selected_tags(qtbot: QtBot, session: KeepSession) -> None:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    panel = window.tag_panel
    qtbot.waitUntil(lambda: panel.loaded, timeout=5000)
    tree = panel.model.tree
    assert tree is not None
    places = next(t for t in tree.children(None) if tree.node(t).name == "Places")
    iceland = tree.children(places)[0]
    mime = panel.model.mimeData([panel.model.index_of(places), panel.model.index_of(iceland)])
    assert dragged_tags(mime) == [places, iceland]
    assert panel.model.flags(panel.model.index_of(places)) & Qt.ItemFlag.ItemIsDragEnabled


# --- the whole flow in a keep window ---


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()
    page = _page(window)
    qtbot.waitUntil(lambda: page.status.text() == "9 items", timeout=5000)
    qtbot.waitUntil(lambda: window.tag_panel.loaded, timeout=5000)
    return window


def _page(window: MainWindow) -> SearchPage:
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    return page


def _tag(window: MainWindow, name: str) -> int:
    tree = window.tag_panel.model.tree
    assert tree is not None
    [tag_id] = [s.tag_id for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
    return tag_id


def _row(page: SearchPage, title: str) -> int:
    titles = [page.model.index(r, 0).data() for r in range(page.model.rowCount())]
    return titles.index(title)


def _tagged(session: KeepSession, tag_id: int) -> set[str]:
    tree = session.tag_cache.get()
    with session.reader.connect() as conn:
        return {h.title for h in run_search(conn, SearchSpec(include=(tag_id,)), tree)}


def _status(window: MainWindow) -> str:
    return window.statusBar().currentMessage()


def test_dropping_on_a_selection_tags_all_of_it_and_undo_reverses_it(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page(window)
    favorites = _tag(window, "Favorites")
    before = _tagged(session, favorites)
    rows = [_row(page, "notes.txt"), _row(page, "readme.md")]
    _select(page.table, rows)
    _drop(page.table, rows[1], [favorites])
    qtbot.waitUntil(lambda: _status(window) == "Tagged 2 items with 'Favorites'.", timeout=5000)
    assert _tagged(session, favorites) == before | {"notes.txt", "readme.md"}
    assert window.undo_action.isEnabled()
    assert window.undo_action.text() == "Undo Tag 2 items with 'Favorites'"

    window.undo_action.trigger()
    qtbot.waitUntil(lambda: _status(window).startswith("Undid:"), timeout=5000)
    assert _tagged(session, favorites) == before
    assert not window.undo_action.isEnabled()
    assert window.redo_action.text() == "Redo Tag 2 items with 'Favorites'"
    window.redo_action.trigger()
    qtbot.waitUntil(lambda: _status(window).startswith("Redid:"), timeout=5000)
    assert _tagged(session, favorites) == before | {"notes.txt", "readme.md"}


def test_dropping_outside_the_selection_tags_just_that_row(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page(window)
    sky = _tag(window, "Sky")
    _select(page.table, [_row(page, "notes.txt"), _row(page, "readme.md")])
    _drop(page.table, _row(page, "tax 2025.pdf"), [sky])
    qtbot.waitUntil(lambda: _status(window) == "Tagged 1 item with 'Sky'.", timeout=5000)
    assert "tax 2025.pdf" in _tagged(session, sky)
    assert "notes.txt" not in _tagged(session, sky)


def test_dropping_several_tags_and_a_drop_that_changes_nothing(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page(window)
    tags = [_tag(window, "Sky"), _tag(window, "Money")]
    _drop(page.table, _row(page, "readme.md"), tags)
    qtbot.waitUntil(
        lambda: _status(window) == "Tagged 1 item with 'Sky' and 'Money'.", timeout=5000
    )
    _drop(page.table, _row(page, "readme.md"), tags)
    qtbot.waitUntil(lambda: _status(window).startswith("Already tagged"), timeout=5000)
    assert window.undo_action.text() == "Undo Tag 1 item with 'Money', 'Sky'"  # not a new step


def test_tagging_refreshes_a_tag_filtered_search(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page(window)
    readme = page.model.hit(_row(page, "readme.md"))
    assert readme is not None
    sky = _tag(window, "Sky")
    page.filter_bar.add_tag(sky)  # sunset.jpg and northern lights.png
    qtbot.waitUntil(lambda: page.status.text() == "2 items", timeout=5000)
    window.apply_tags([readme.id], [sky])  # as a drop on another page would
    qtbot.waitUntil(lambda: page.status.text() == "3 items", timeout=5000)
    assert "readme.md" in {page.model.index(r, 0).data() for r in range(3)}


def test_non_tag_drops_are_ignored(qtbot: QtBot, window: MainWindow) -> None:
    page = _page(window)
    text = QMimeData()
    text.setText("hello")
    with qtbot.assertNotEmitted(page.table.tags_dropped):
        _drop(page.table, 0, None, mime=text)


def test_ids_for_rows_that_are_not_loaded(qtbot: QtBot, session: KeepSession) -> None:
    model = ResultsModel(session, page_size=2, pool=QThreadPool())
    with qtbot.waitSignal(model.counted, timeout=5000):
        model.set_search(SearchSpec(), [ResultColumn("title", "Name")])
    assert model.loaded_pages() == [0]
    got: list[list[int]] = []
    model.entity_ids([0, 5, 8, 1], got.append)  # rows 5 and 8 aren't loaded
    qtbot.waitUntil(lambda: bool(got), timeout=5000)
    ids = got[0]
    assert len(ids) == 4
    assert len(set(ids)) == 4
    with session.reader.connect() as conn:
        all_ids = [h.id for h in run_search(conn, SearchSpec(), session.tag_cache.get())]
    assert ids == [all_ids[0], all_ids[1], all_ids[5], all_ids[8]]  # in row order


def test_a_section_drop_reports_its_entities(qtbot: QtBot) -> None:
    hits = [SearchHit(10, "t", "A"), SearchHit(11, "t", "B"), SearchHit(12, "t", "C")]
    group = TypeGroup(
        "t", "Things", 3, (ResultColumn("title", "Title"),), tuple((h, {}) for h in hits)
    )
    section = TypeSection(group)
    qtbot.addWidget(section)
    section.show()
    _select(section.table, [0, 2])
    with qtbot.waitSignal(section.tags_dropped) as blocker:
        _drop(section.table, 2, [7])
    assert blocker.args == [[10, 12], [7]]


def _move(table: ResultTable, row: int, tag_ids: list[int]) -> bool:
    """Drag ``tag_ids`` over ``row``; returns whether the view would accept a drop there."""
    mime = tags_mime(tag_ids)  # the event only points at it: keep it alive meanwhile
    y = table.rowViewportPosition(row) + table.rowHeight(row) // 2
    event = QDragMoveEvent(
        QPoint(20, y),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    table.dragMoveEvent(event)
    return event.isAccepted()


def test_dragging_outlines_the_rows_a_drop_would_tag(qtbot: QtBot, window: MainWindow) -> None:
    table = _page(window).table
    height = table.rowHeight(0)
    _select(table, [1, 2, 5])
    assert _move(table, 2, [1])  # over the selection: all of it, in two runs
    outline = table.drop_outline()
    assert [(r.top(), r.height()) for r in outline] == [
        (table.rowViewportPosition(1), 2 * height),
        (table.rowViewportPosition(5), height),
    ]
    _move(table, 7, [1])  # outside the selection: just that row
    assert [r.top() for r in table.drop_outline()] == [table.rowViewportPosition(7)]
    table.dragLeaveEvent(QDragLeaveEvent())
    assert table.drop_outline() == []
