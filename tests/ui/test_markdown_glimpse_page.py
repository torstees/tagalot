"""A glimpse of an item's own Markdown file on its page (#554, DESIGN.md §12): its table
of contents, else its opening, in the header's note; Open file; a typed note wins."""

import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool, QUrl
from pytestqt.qtbot import QtBot

from tagalot.core.handlers import OPEN, FileToOpen
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.main_window import MainWindow
from tagalot.ui.workers import ScanController
from tests.ui.test_contents_search import SCRIPT, _open

pytestmark = pytest.mark.gui


@pytest.fixture
def files(tmp_path: Path) -> Path:
    return tmp_path / "book-files"


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_books_demo(tmp_path), Settings()) as session:
        window = MainWindow(session, scans=ScanController(QThreadPool()))
        qtbot.addWidget(window)
        window.resize(1200, 800)
        window.show()
        yield window
        window.thumbnails.clear()
        window.thumbnails.wait()


def _page(qtbot: QtBot, window: MainWindow, title: str) -> DetailPage:
    assert window.session is not None
    page = _open(qtbot, window, window.session, title)
    qtbot.waitUntil(page.note_view.isVisible, timeout=5000)
    return page


def test_a_files_table_of_contents(qtbot: QtBot, window: MainWindow) -> None:
    page = _page(qtbot, window, "The Glass Tower")
    note = page.note_view
    qtbot.waitUntil(lambda: note.glimpse, timeout=5000)
    assert note.text == (
        "## Contents\n\n"
        "- [Chapter 1: Morning](#chapter-1-morning)\n"
        "- [Chapter 2: The Stair](#chapter-2-the-stair)"
    )
    # Collapsed, the heading is left out: just the chapters.
    assert note.body.toPlainText() == "Chapter 1: Morning\nChapter 2: The Stair"
    assert note.open_button.isVisible()
    assert note.open_button.text() == "Open file"
    assert note.add_button.isVisible()  # a note can still be typed
    assert not note.pencil.isVisible()  # but the file isn't edited here
    assert not page.open_note_action.isVisible()


def test_a_files_opening(qtbot: QtBot, window: MainWindow) -> None:
    note = _page(qtbot, window, "The Lighthouse Keeper").note_view
    qtbot.waitUntil(lambda: note.glimpse, timeout=5000)
    assert note.text == ("Written on a train.\n\n# The Lighthouse Keeper\n\nIt was a dark night.")
    assert note.body.toPlainText() == "Written on a train.\nIt was a dark night."


def test_open_file(
    qtbot: QtBot, window: MainWindow, files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acted: list[tuple[str, str]] = []

    def act(_opener: FileOpener, file: object, how: str) -> None:
        assert isinstance(file, FileToOpen), file
        acted.append((file.path, how))

    monkeypatch.setattr(FileOpener, "act", act)
    page = _page(qtbot, window, "The Glass Tower")
    qtbot.waitUntil(lambda: page.note_view.glimpse, timeout=5000)
    page.note_view.open_button.click()
    qtbot.waitUntil(lambda: bool(acted), timeout=5000)
    # A link to one of the file's headings (its table of contents) opens the file too.
    page.note_view.body.anchorClicked.emit(QUrl("#chapter-2-the-stair"))
    qtbot.waitUntil(lambda: len(acted) == 2, timeout=5000)
    tower = os.path.normpath(files / "Writing" / "The Glass Tower" / "The Glass Tower.md")
    assert [(os.path.normpath(path), how) for path, how in acted] == [(tower, OPEN)] * 2


def test_a_typed_note_takes_its_place(qtbot: QtBot, window: MainWindow) -> None:
    page = _page(qtbot, window, "The Glass Tower")
    note = page.note_view
    qtbot.waitUntil(lambda: note.glimpse, timeout=5000)
    note.add_button.click()
    assert note.box.toPlainText() == ""  # not the file's text
    note.box.setPlainText("My own words about it.")
    note._submit()
    assert not note.glimpse
    assert not note.open_button.isVisible()
    # Saved, and read back as the item's note: it wins over the file.
    page.refresh()
    qtbot.waitUntil(
        lambda: not note.glimpse and note.body.toPlainText() == "My own words about it.",
        timeout=5000,
    )
    assert note.pencil.isVisible()


def test_nothing_when_the_file_cant_be_read(qtbot: QtBot, window: MainWindow, files: Path) -> None:
    page = _page(qtbot, window, "The Glass Tower")
    qtbot.waitUntil(lambda: page.note_view.glimpse, timeout=5000)
    (files / "Writing" / "The Glass Tower" / "The Glass Tower.md").unlink()
    page.refresh()
    qtbot.waitUntil(lambda: not page.note_view.glimpse, timeout=5000)
    assert page.note_view.add_button.isVisible()
    assert not page.note_view.body.isVisible()
