"""Tests for the results model and the search page (list layout)."""

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from pytestqt.qtbot import QtBot

from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.search_spec import SearchSpec, SortKey
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.main_window import MainWindow
from tagalot.ui.models.results import PLACEHOLDER, ResultColumn, ResultsModel, display_value
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage, count_text, list_columns
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

NAMES = [f"file{i:02}.txt" for i in range(10)]  # file00.txt … file09.txt
COLUMNS = [ResultColumn("title", "Name"), ResultColumn("extension", "Extension")]


@pytest.fixture
def files(tmp_path: Path) -> Path:
    folder = tmp_path / "files"
    folder.mkdir()
    for i, name in enumerate(NAMES):
        path = folder / name
        path.write_bytes(b"x" * i)
        stamp = 1_700_000_000 + (i * 7 % 10) * 3600  # modified order differs from name order
        os.utime(path, (stamp, stamp))
    return folder


@pytest.fixture
def session(tmp_path: Path, files: Path) -> Iterator[KeepSession]:
    roots = [RootConfig("r", "Files", str(files))]
    keep_dir = create_keep(tmp_path / "Files.keep", "Files", ThemeRef("generic", 1), roots).dir
    with KeepSession.open(keep_dir, Settings()) as session:
        session.scan_all()
        yield session


@pytest.fixture
def pool() -> QThreadPool:
    return QThreadPool()


def _model(
    session: KeepSession, pool: QThreadPool, *, page_size: int = 100, max_pages: int = 50
) -> ResultsModel:
    return ResultsModel(
        session,
        type_labels={"generic.file": "File"},
        page_size=page_size,
        max_pages=max_pages,
        pool=pool,
    )


def _search(qtbot: QtBot, model: ResultsModel, spec: SearchSpec | None = None) -> int:
    with qtbot.waitSignal(model.counted, timeout=5000) as blocker:
        model.set_search(spec or SearchSpec(), COLUMNS)
    total: int = blocker.args[0]
    return total


def _titles(model: ResultsModel) -> list[str]:
    return [model.index(r, 0).data() for r in range(model.rowCount())]


def test_count_and_first_page(qtbot: QtBot, session: KeepSession, pool: QThreadPool) -> None:
    model = _model(session, pool, page_size=4)
    assert _search(qtbot, model) == 10
    assert (model.rowCount(), model.columnCount()) == (10, 2)
    assert model.loaded_pages() == [0]
    assert model.index(0, 0).data() == "file00.txt"
    assert model.index(0, 1).data() == ".txt"
    assert model.headerData(1, Qt.Orientation.Horizontal) == "Extension"
    assert not model.searching


def test_later_pages_load_on_demand(qtbot: QtBot, session: KeepSession, pool: QThreadPool) -> None:
    model = _model(session, pool, page_size=4)
    _search(qtbot, model)
    assert model.index(9, 0).data() == PLACEHOLDER  # page 2 isn't loaded yet
    assert model.index(9, 1).data() == ""
    with qtbot.waitSignal(model.dataChanged, timeout=5000) as blocker:
        pass
    top, bottom = blocker.args[:2]
    assert (top.row(), bottom.row(), bottom.column()) == (8, 9, 1)  # the short last page
    assert model.index(9, 0).data() == "file09.txt"
    assert model.loaded_pages() == [0, 2]
    assert model.hit(9) is not None
    assert model.hit(5) is None  # page 1 was never shown


def test_only_recent_pages_are_kept(qtbot: QtBot, session: KeepSession, pool: QThreadPool) -> None:
    model = _model(session, pool, page_size=2, max_pages=2)
    _search(qtbot, model)
    for row in (2, 4, 6):
        model.index(row, 0).data()
        with qtbot.waitSignal(model.dataChanged, timeout=5000):
            pass
        assert model.hit(row) is not None
    assert model.loaded_pages() == [2, 3]
    assert model.index(0, 0).data() == PLACEHOLDER  # dropped, so fetched again
    qtbot.waitUntil(lambda: model.index(0, 0).data() == "file00.txt", timeout=5000)


def test_every_row_loads(qtbot: QtBot, session: KeepSession, pool: QThreadPool) -> None:
    model = _model(session, pool, page_size=3)
    _search(qtbot, model)
    _titles(model)
    qtbot.waitUntil(lambda: PLACEHOLDER not in _titles(model), timeout=5000)
    assert _titles(model) == NAMES


def test_a_newer_search_wins(qtbot: QtBot, session: KeepSession, pool: QThreadPool) -> None:
    model = _model(session, pool)
    model.set_search(SearchSpec(), COLUMNS)
    with qtbot.waitSignal(model.counted, timeout=5000) as blocker:
        model.set_search(SearchSpec(text="file03"))
    assert blocker.args == [1]
    qtbot.wait(100)  # the first search's results arrive, and are dropped
    assert _titles(model) == ["file03.txt"]
    assert [c.label for c in model.columns] == ["Name", "Extension"]  # kept


def test_sorting(qtbot: QtBot, session: KeepSession, pool: QThreadPool) -> None:
    model = _model(session, pool)
    _search(qtbot, model)
    assert model.sort_column() == (0, Qt.SortOrder.AscendingOrder)
    with qtbot.waitSignal(model.counted, timeout=5000):
        model.sort(0, Qt.SortOrder.DescendingOrder)
    assert model.spec is not None
    assert model.spec.sort == (SortKey("title", descending=True),)
    assert _titles(model)[:2] == ["file09.txt", "file08.txt"]
    assert model.sort_column() == (0, Qt.SortOrder.DescendingOrder)


def test_columns_that_cant_sort_are_ignored(
    qtbot: QtBot, session: KeepSession, pool: QThreadPool
) -> None:
    model = _model(session, pool)
    with qtbot.waitSignal(model.counted, timeout=5000):
        model.set_search(
            SearchSpec(),
            [ResultColumn("title", "Name"), ResultColumn("type", "Type", sortable=False)],
        )
    assert model.index(0, 1).data() == "File"
    spec = model.spec
    model.sort(1)
    assert model.spec is spec
    assert not model.searching


def test_sorting_by_a_theme_field(qtbot: QtBot, session: KeepSession, pool: QThreadPool) -> None:
    model = _model(session, pool)
    spec = SearchSpec(types=("generic.file",), sort=(SortKey("modified", descending=True),))
    columns = [ResultColumn("title", "Name"), ResultColumn("modified", "Modified")]
    with qtbot.waitSignal(model.counted, timeout=5000):
        model.set_search(spec, columns)
    # Name i was modified at hour (i * 7 % 10): newest is file07 (hour 9), then file04 (8).
    assert _titles(model)[:3] == ["file07.txt", "file04.txt", "file01.txt"]
    assert model.sort_column() == (1, Qt.SortOrder.DescendingOrder)


def test_a_bad_search_reports_a_message(
    qtbot: QtBot, session: KeepSession, pool: QThreadPool
) -> None:
    model = _model(session, pool)
    with qtbot.waitSignal(model.failed, timeout=5000) as blocker:
        model.set_search(SearchSpec(sort=(SortKey("rating"),)), COLUMNS)
    assert "rating" in blocker.args[0]
    assert model.rowCount() == 0
    assert not model.searching


def test_refresh_keeps_rows_until_new_ones_arrive(
    qtbot: QtBot, session: KeepSession, pool: QThreadPool, files: Path
) -> None:
    model = _model(session, pool)
    _search(qtbot, model)
    (files / "new.txt").write_bytes(b"new")
    session.scan_all()
    with qtbot.waitSignal(model.counted, timeout=5000) as blocker:
        model.refresh()
        assert model.rowCount() == 10  # still showing the old rows
    assert blocker.args == [11]
    assert "new.txt" in _titles(model)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        (True, "Yes"),
        (1999, "1999"),
        (2.0, "2"),
        (2.5, "2.5"),
        (date(2024, 2, 29), "2024-02-29"),
        ("txt", "txt"),
    ],
)
def test_display_value(value: object, text: str) -> None:
    assert display_value(value) == text


def test_datetimes_show_in_local_time() -> None:
    when = datetime(2026, 9, 27, 12, 30, tzinfo=UTC)
    assert display_value(when) == when.astimezone().strftime("%Y-%m-%d %H:%M")


def test_count_text() -> None:
    assert [count_text(n) for n in (1, 2, 12345)] == ["1 item", "2 items", "12,345 items"]


# --- the search page ---


def test_list_columns(session: KeepSession) -> None:
    columns = list_columns(session.schema, ("generic.file",))
    assert [(c.key, c.label, c.numeric) for c in columns] == [
        ("title", "Name", False),  # the type's title_label
        ("tags", "Tags", False),  # the item's own tags, next to the title; hidden by default
        ("extension", "Extension", False),
        ("folder", "Folder", False),
        ("size", "Size", True),  # right-aligned
        ("modified", "Modified", False),
    ]  # then the card fields, in declaration order
    everything = [c.key for c in list_columns(session.schema, ())]
    assert everything == ["title", "tags", "extension", "folder", "size", "modified"]


def test_search_page(qtbot: QtBot, session: KeepSession, pool: QThreadPool) -> None:
    page = SearchPage(session, "Search all", SearchSpec(), pool=pool)
    qtbot.addWidget(page)
    page.show()
    qtbot.waitUntil(lambda: page.status.text() == "10 items", timeout=5000)
    header = page.table.horizontalHeader()
    assert (header.sortIndicatorSection(), header.sortIndicatorOrder()) == (
        0,
        Qt.SortOrder.AscendingOrder,
    )
    header.setSortIndicator(0, Qt.SortOrder.DescendingOrder)  # as a header click does
    qtbot.waitUntil(lambda: page.model.index(0, 0).data() == "file09.txt", timeout=5000)
    assert page.status.text() == "10 items"


def test_search_page_nothing_found(qtbot: QtBot, session: KeepSession, pool: QThreadPool) -> None:
    page = SearchPage(session, "Search all", SearchSpec(text="zebra"), pool=pool)
    qtbot.addWidget(page)
    qtbot.waitUntil(lambda: page.status.text() == "Nothing found", timeout=5000)


def test_the_window_shows_searches_and_refreshes_them_after_a_scan(
    qtbot: QtBot, session: KeepSession, files: Path
) -> None:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.show()
    window.navigation.select(NavTarget("view", key="Recently modified", label="Recently modified"))
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    qtbot.waitUntil(lambda: page.status.text() == "10 items", timeout=5000)
    assert page.model.index(0, 0).data() == "file07.txt"
    assert page.model.sort_column() == (5, Qt.SortOrder.DescendingOrder)  # after Tags

    (files / "new.txt").write_bytes(b"new")
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    qtbot.waitUntil(lambda: page.status.text() == "11 items", timeout=5000)
    assert len(window.search_pages()) == 2  # Search all, then Recently modified
