"""Triage's Pictures tab (#426), on the books demo: *The Lighthouse Keeper* names a cover
that isn't there, and *Field Notes*' cover is damaged (so its picture of its own, #446,
can't be drawn either)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.handlers import REVEAL, FileToOpen
from tagalot.core.models import Entity
from tagalot.core.picture_problems import CANT_DRAW, NOT_FOUND, NOT_SCANNED, NOT_THERE
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.triage import PICTURES_HINT, PICTURES_TAB, TriagePage
from tests.core.book_files import jpeg
from tests.ui.test_file_extra_page import window

pytestmark = pytest.mark.gui

__all__ = ["window"]  # fixtures


def _triage(qtbot: QtBot, window: MainWindow, count: int) -> TriagePage:
    window.navigation.select(NavTarget("triage", label="Triage"))
    triage = window.stack.currentWidget()
    assert isinstance(triage, TriagePage)
    triage.counts = None
    triage.refresh()
    qtbot.waitUntil(lambda: triage.counts is not None, timeout=5000)
    assert triage.tabs.tabText(PICTURES_TAB) == f"Pictures ({count})"
    triage.tabs.setCurrentIndex(PICTURES_TAB)
    return triage


def _draw_field_notes(window: MainWindow) -> None:
    """Draw Field Notes' thumbnail, as the grid does, so its damaged cover is recorded."""
    session = window.session
    assert session is not None
    with session.reader.connect() as conn:
        book = conn.scalar(select(Entity.id).where(Entity.title == "Field Notes"))
    assert book is not None
    window.thumbnails.resolver.resolve(book)
    session.writer.run(lambda conn: None)  # the failure is queued; let it land


def test_the_pictures_tab_lists_covers_not_found_and_damaged(
    qtbot: QtBot, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    triage = _triage(qtbot, window, 1)  # the damaged cover isn't known until it's drawn
    [lighthouse] = triage.pictures.problems
    assert (lighthouse.kind, lighthouse.title) == (NOT_FOUND, "The Lighthouse Keeper")
    assert [(t.relpath, t.state) for t in lighthouse.tried] == [("Loose/lighthouse.jpg", NOT_THERE)]
    row = [triage.pictures.data(triage.pictures.index(0, c)) for c in range(4)]
    assert row == [
        "The Lighthouse Keeper",
        "Book",
        "lighthouse.jpg: not found",
        "Loose/A Short Story.md",
    ]
    assert triage.picture_details.text() == PICTURES_HINT
    triage.pictures_table.selectRow(0)
    details = triage.picture_details.text()
    assert details.startswith("A Short Story.md names its cover: lighthouse.jpg\n")
    assert f"1. Loose/lighthouse.jpg: {NOT_THERE}" in details

    _draw_field_notes(window)
    triage = _triage(qtbot, window, 3)
    kinds = {p.title: p.kind for p in triage.pictures.problems}
    assert kinds == {
        "Field Notes": CANT_DRAW,
        "field-notes.jpg": CANT_DRAW,  # the same file, as a Picture (#446)
        "The Lighthouse Keeper": NOT_FOUND,
    }
    [notes] = [p for p in triage.pictures.problems if p.title == "Field Notes"]
    assert notes.relpath == "Loose/field-notes.jpg"
    triage.pictures_table.selectRow(triage.pictures.problems.index(notes))
    assert triage.picture_details.text().startswith("field-notes.jpg can't be drawn: ")

    opened: list[tuple[FileToOpen, str]] = []
    monkeypatch.setattr(FileOpener, "act", lambda self, file, how: opened.append((file, how)))
    triage._show_picture_file()
    assert [(f.path.endswith("field-notes.jpg"), how) for f, how in opened] == [(True, REVEAL)]
    reread: list[list[int]] = []
    triage.reread_requested.connect(reread.append)
    triage._reread_picture_item()
    assert reread == [[notes.entity_id]]
    triage._open_picture_item()
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: page.detail is not None, timeout=5000)
    assert page.entity_id == notes.entity_id


def test_adding_the_cover_and_scanning_clears_it(qtbot: QtBot, window: MainWindow) -> None:
    session = window.session
    assert session is not None
    triage = _triage(qtbot, window, 1)
    [lighthouse] = triage.pictures.problems
    picture = Path(session.root_path(lighthouse.root_id)) / "Loose/lighthouse.jpg"
    picture.write_bytes(jpeg())
    triage = _triage(qtbot, window, 1)
    assert [t.state for t in triage.pictures.problems[0].tried] == [NOT_SCANNED]
    session.scan_all()  # the picture is read after its book, which names it: linked
    triage = _triage(qtbot, window, 0)
    assert triage.pictures_stack.currentIndex() == 1  # the empty state


def test_an_older_refresh_finishing_last_is_not_shown(
    qtbot: QtBot, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refreshes overlap (a change, then Show ignored): the latest one's rows stay (#406)."""
    triage = _triage(qtbot, window, 1)
    jobs: list[tuple[Callable[[], Any], Callable[[Any], None]]] = []
    monkeypatch.setattr(
        "tagalot.ui.triage.run_in_pool", lambda job, on_done: jobs.append((job, on_done))
    )
    triage.show_ignored.setChecked(True)  # a refresh
    triage.show_ignored.setChecked(False)  # and another
    [(_, older_done), (newer, newer_done)] = jobs
    newer_done(newer())
    assert len(triage.pictures.problems) == 1
    counts, files, notes, _, skipped, new, links = newer()
    older_done((counts, files, notes, [], skipped, new, links))  # what it saw, different, last
    assert len(triage.pictures.problems) == 1


def test_ignoring_rows_from_the_tab(qtbot: QtBot, window: MainWindow) -> None:
    _draw_field_notes(window)
    triage = _triage(qtbot, window, 3)
    titles = [p.title for p in triage.pictures.problems]
    triage.pictures_table.selectRow(titles.index("The Lighthouse Keeper"))
    assert triage.ignore_value.text() == "“lighthouse.jpg” in every file"
    assert triage.ignore_type.text() == "Every Book"
    assert not triage.stop_ignoring_button.isVisible()
    with qtbot.waitSignal(window.tag_actions.changed):
        triage.ignore_item.trigger()  # as choosing Ignore → For this item does
    assert (
        window.statusBar()
        .currentMessage()
        .startswith("Ignore The Lighthouse Keeper's missing cover.")
    )
    triage = _triage(qtbot, window, 2)
    titles = [p.title for p in triage.pictures.problems]
    assert sorted(titles) == ["Field Notes", "field-notes.jpg"]

    triage.pictures_table.selectRow(titles.index("Field Notes"))  # damaged: no value to ignore
    assert not triage.ignore_value.isEnabled()

    triage.show_ignored.setChecked(True)
    qtbot.waitUntil(lambda: len(triage.pictures.problems) == 3, timeout=5000)
    assert triage.tabs.tabText(PICTURES_TAB) == "Pictures (2)"  # ignored rows aren't counted
    row = [p.title for p in triage.pictures.problems].index("The Lighthouse Keeper")
    assert triage.pictures.data(triage.pictures.index(row, 2)) == (
        "lighthouse.jpg: not found (ignored for this item)"
    )
    triage.pictures_table.selectRow(row)
    assert triage.stop_ignoring_button.isVisible()
    assert triage.stop_ignoring_button.isEnabled()
    assert not triage.ignore_button.isEnabled()
    with qtbot.waitSignal(window.tag_actions.changed):
        triage.stop_ignoring_button.click()
    triage.show_ignored.setChecked(False)
    _triage(qtbot, window, 3)

    window.tag_actions.undo()  # Stop ignoring, undone: ignored again
    qtbot.waitUntil(lambda: window.tag_actions.busy == 0, timeout=5000)
    _triage(qtbot, window, 2)
