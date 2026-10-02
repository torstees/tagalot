"""The Dedupe page: exact duplicates and checking them (#117), on the music demo."""

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QTableWidget
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.ui.dedupe_view import CHECK, ITEMS, DedupePage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tests.ui.test_music_views import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


def test_a_copied_song_is_found_checked_and_shown(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
) -> None:
    root = Path(session.root_path("music"))
    copy = root / "Copies" / "Blue (copy).mp3"
    copy.parent.mkdir()
    shutil.copy(root / "Jazz Hits" / "02 Blue.mp3", copy)
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()

    window.navigation.select(NavTarget("dedupe", label="Dedupe"))
    page = window.stack.currentWidget()
    assert isinstance(page, DedupePage)
    qtbot.waitUntil(lambda: page.groups is not None, timeout=5000)
    assert page.summary.text().startswith("1 group of identical files")
    group = page.model.item(0, 0)
    assert group.text().endswith("\u00d72")
    assert group.rowCount() == 2
    assert {group.child(n, ITEMS).text() for n in range(2)} == {"Blue"}

    assert not page.check_button.isEnabled()  # nothing selected yet
    assert "Select a group" in page.check_button.toolTip()
    assert page.check_all_button.isEnabled()
    page.tree.selectionModel().select(
        page.model.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    assert page.check_button.isEnabled()
    page.check_button.click()
    qtbot.waitUntil(lambda: page.model.item(0, CHECK).text() == "identical", timeout=5000)
    assert [group.child(n, CHECK).text() for n in range(2)] == ["same", "same"]

    copy_row = next(n for n in range(2) if "Copies" in group.child(n, 1).text())
    file = page._file_at(page.model.index(copy_row, 0, page.model.index(0, 0)))
    assert file is not None
    with qtbot.waitSignal(page.open_entity) as blocker:
        page._activated(page.model.index(copy_row, 0, page.model.index(0, 0)))
    assert blocker.args == [file.items[0][0]]


def test_check_is_greyed_out_without_a_selection(qtbot: QtBot, window: MainWindow) -> None:
    window.navigation.select(NavTarget("dedupe", label="Dedupe"))
    page = window.stack.currentWidget()
    assert isinstance(page, DedupePage)
    qtbot.waitUntil(lambda: page.groups is not None, timeout=5000)
    assert page.groups == []
    assert not page.check_button.isEnabled()
    assert not page.check_all_button.isEnabled()  # nothing to check at all


def test_similar_items(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    root = Path(session.root_path("music"))
    copy = root / "Copies" / "Take Five.mp3"
    copy.parent.mkdir()
    shutil.copy(root / "Jazz Hits" / "01 Take Five.mp3", copy)
    copy.write_bytes(copy.read_bytes() + b"\0" * 10)  # not identical: only alike
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    window.navigation.select(NavTarget("dedupe", label="Dedupe"))
    page = window.stack.currentWidget()
    assert isinstance(page, DedupePage)
    qtbot.waitUntil(lambda: page.pairs is not None, timeout=5000)
    assert page.tabs.tabText(1) == "Similar items (1)"
    assert page.similar_summary.text().startswith("1 pair of items that look alike")
    cells = [page.similar.item(0, n).text() for n in range(4)]
    assert cells == ["100%", "Song", "Take Five", "Take Five"]
    with qtbot.waitSignal(page.open_entity):
        page._open_pair(0, first=True)
    assert page.compare_pane.comparison is None  # nothing current yet
    assert "Select a group" in page.compare_pane.summary.text()

    page.tabs.setCurrentIndex(1)
    page.similar_table.setCurrentIndex(page.similar.index(0, 0))
    pane = page.compare_pane
    qtbot.waitUntil(lambda: pane.comparison is not None, timeout=5000)
    assert pane.summary.text().startswith("100% alike. Highlighted rows differ.")
    assert pane.table.columnCount() == 2
    headers = [pane.table.horizontalHeaderItem(n) for n in range(2)]
    assert [h.text() for h in headers if h is not None] == ["Take Five"] * 2
    labels = [pane.table.verticalHeaderItem(n).text() for n in range(pane.table.rowCount())]
    assert labels[:3] == ["", "Type", "Title"]
    files = labels.index("Files")
    assert _cell(pane.table, files, 0) != _cell(pane.table, files, 1)
    assert pane.table.verticalHeaderItem(files).font().bold()  # differs
    assert not pane.table.verticalHeaderItem(labels.index("Type")).font().bold()
    with qtbot.waitSignal(page.open_entity) as blocker:
        pane._open_column(1)
    assert pane.comparison is not None
    assert blocker.args == [pane.comparison.items[1].id]

    shown = pane.comparison
    page.refresh()  # the same pair stays current
    qtbot.waitUntil(
        lambda: pane.comparison is not None and pane.comparison is not shown, timeout=5000
    )
    assert page.similar_table.currentIndex().row() == 0


def _cell(table: QTableWidget, row: int, column: int) -> str:
    item = table.item(row, column)
    assert item is not None
    return item.text()


def test_comparing_a_group(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    root = Path(session.root_path("music"))
    copy = root / "Copies" / "Blue (copy).mp3"
    copy.parent.mkdir()
    shutil.copy(root / "Jazz Hits" / "02 Blue.mp3", copy)
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    window.navigation.select(NavTarget("dedupe", label="Dedupe"))
    page = window.stack.currentWidget()
    assert isinstance(page, DedupePage)
    qtbot.waitUntil(lambda: page.groups is not None, timeout=5000)
    page.tree.setCurrentIndex(page.model.index(0, 0, page.model.index(0, 0)))  # a copy's row
    pane = page.compare_pane
    qtbot.waitUntil(lambda: pane.comparison is not None, timeout=5000)
    assert pane.summary.text().startswith("2 items use these copies.")
    found = pane.comparison
    assert found is not None
    assert [i.title for i in found.items] == ["Blue", "Blue"]

    shown = pane.comparison
    page.refresh()  # the lists are rebuilt (after tagging, say): the group stays current
    qtbot.waitUntil(
        lambda: pane.comparison is not None and pane.comparison is not shown, timeout=5000
    )
    assert page.selected_groups() == page.groups
    assert pane.comparison is not None
    assert [i.title for i in pane.comparison.items] == ["Blue", "Blue"]
