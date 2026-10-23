"""Kind chips (#513): hiding and showing file types and kinds of items in a list, in place,
remembered per view and tab.

The demo keep's Files view lists 4 JPGs, 2 text files, a PDF, a PNG, and a Markdown file.
"""

import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from pytestqt.qtbot import QtBot

from tagalot.core.search import count_by_kind, run_search
from tagalot.core.search_fields import search_fields
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.kind_chips import KindChips, kind_label, kind_tip
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import ScanController
from tests.ui.test_grid import SCRIPT, _close, _page, _window, keep_dir, session

pytestmark = pytest.mark.gui

__all__ = ["keep_dir", "session"]  # fixtures

KINDS = {".jpg": 4, ".md": 1, ".pdf": 1, ".png": 1, ".txt": 2}
FOLDER, BOOK = "books.folder", "books.book"


def _chips(qtbot: QtBot, page: SearchPage) -> KindChips:
    chips = page.kind_chips
    qtbot.waitUntil(lambda: not chips.isHidden() and bool(chips.chips), timeout=5000)
    return chips


def _status(qtbot: QtBot, page: SearchPage, expected: str) -> None:
    def check() -> None:
        assert page.status.text() == expected

    qtbot.waitUntil(check, timeout=5000)


def _titles(page: SearchPage) -> set[str]:
    hits = (page.model.hit(r) for r in range(page.model.rowCount()))
    return {h.title for h in hits if h is not None}


def test_hiding_and_showing_kinds(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    page = _page(window)
    chips = _chips(qtbot, page)
    assert {k: int(c.text().rsplit(" ", 1)[1]) for k, c in chips.chips.items()} == KINDS
    jpg = chips.chips[".jpg"]
    assert jpg.text() == "JPG 4"
    assert jpg.isChecked()
    assert jpg.toolTip().startswith("Hide JPG files")

    jpg.click()
    _status(qtbot, page, "5 items")
    assert "glacier.jpg" not in _titles(page)
    assert chips.chips[".jpg"] is jpg  # stays, switched off
    assert not jpg.isChecked()
    assert jpg.font().strikeOut()
    assert jpg.text() == "JPG 4"  # counted all the same
    assert jpg.toolTip().startswith("Show JPG files")
    assert list(window._ui_state["hidden_kinds"].values()) == [[".jpg"]]

    chips.toggle(".pdf", only=True)  # Ctrl+click: only PDFs
    _status(qtbot, page, "1 item")
    assert _titles(page) == {"tax 2025.pdf"}
    chips.toggle(".pdf", only=True)  # and again: everything
    _status(qtbot, page, "9 items")
    assert chips.hidden == ()
    assert window._ui_state["hidden_kinds"] == {}  # nothing to remember

    for kind in KINDS:
        chips.toggle(kind)
    qtbot.waitUntil(page.showing_empty, timeout=5000)
    assert page.empty_text() == ("All kinds are hidden", "Switch a kind back on above.")
    assert not chips.isHidden()  # so they can come back
    chips.toggle(".txt")
    _status(qtbot, page, "2 items")
    assert not page.showing_empty()
    _close(window)

    # Remembered for the view.
    again = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(again)
    again.show()
    page = _page(again)
    _status(qtbot, page, "2 items")
    assert set(page.kind_chips.hidden) == set(KINDS) - {".txt"}
    _close(again)


def test_one_kind_gives_no_chips(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    page = _page(window)
    page.filter_bar.set_text("glacier")
    _status(qtbot, page, "1 item")
    qtbot.waitUntil(lambda: page.kind_chips.chips.keys() == {".jpg"}, timeout=5000)
    assert page.kind_chips.isHidden()
    _close(window)


LABELS = {
    ".pdf": "PDF",
    ".": "No extension",
    FOLDER: "Folders",
    BOOK: "Books",
}


@pytest.mark.parametrize(("kind", "label"), LABELS.items())
def test_labels(kind: str, label: str) -> None:
    plurals = {FOLDER: "Folders", BOOK: "Books"}
    assert kind_label(kind, plurals) == label


def test_the_pages_own_type_is_no_file_unless_contents_show() -> None:
    plurals = {BOOK: "Books"}
    assert kind_label(BOOK, plurals, (BOOK,)) == "No file"
    assert kind_label(BOOK, plurals, (BOOK,), contents=True) == "Books"
    assert kind_tip(BOOK, "No file", plurals, True).startswith("Hide books without a file")
    assert kind_tip(".", "No extension", {}, False).startswith("Show files without an extension")


@pytest.fixture
def books(qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
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
        _close(window)


def _view(qtbot: QtBot, window: MainWindow, name: str) -> SearchPage:
    window.navigation.select(NavTarget("view", key=name, label=name))
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    return page


def test_folders_with_their_contents(qtbot: QtBot, books: MainWindow) -> None:
    """The Folders view with Contained: folders are a kind among the files (#513)."""
    page = _view(qtbot, books, "Folders")
    assert page.kind_chips.isHidden()  # folders only
    page.filter_bar.contained_box.click()
    chips = _chips(qtbot, page)
    qtbot.waitUntil(lambda: FOLDER in chips.chips and ".epub" in chips.chips, timeout=5000)
    assert chips.chips[FOLDER].text() == "Folders 8"
    chips.toggle(FOLDER)

    def no_folders() -> bool:
        hits = [page.model.hit(r) for r in range(page.model.rowCount())]
        return bool(hits) and all(h is not None and h.type != FOLDER for h in hits)

    qtbot.waitUntil(no_folders, timeout=5000)

    # In the tree, folders holding shown items stay, dimmed.
    page.tree_button.click()

    def dimmed_folder() -> bool:
        for row in range(page.tree_model.rowCount()):
            index = page.tree_model.index(row, 0)
            hit = page.tree_model.hit(index)
            if hit is not None and hit.type == FOLDER:
                return index.data(Qt.ItemDataRole.ForegroundRole) is not None
        return False

    qtbot.waitUntil(dimmed_folder, timeout=5000)


def test_a_contents_tab_counts_what_shows(qtbot: QtBot, books: MainWindow) -> None:
    session = books.session
    assert session is not None
    tree = session.tag_cache.get()
    fields = search_fields(session.schema, (BOOK,))
    with session.reader.connect() as conn:
        listed = run_search(conn, SearchSpec(types=(FOLDER,)), tree, limit=None)
        folders = [
            f.id
            for f in listed
            if len(count_by_kind(conn, SearchSpec(types=(BOOK,), within=f.id), tree, fields)) > 1
        ]
    assert folders, "a demo folder holds books of two file types"
    books.open_entity(folders[0])
    detail = books.stack.currentWidget()
    assert isinstance(detail, DetailPage)
    qtbot.waitUntil(lambda: BOOK in detail.contents_pages, timeout=5000)
    page = detail.contents_pages[BOOK]
    chips = _chips(qtbot, page)
    total = page.unhidden_total
    assert total is not None
    chips.toggle(next(iter(chips.chips)))
    tabs = detail._tabs
    assert tabs is not None

    def labelled() -> None:
        text = tabs.tabText(tabs.indexOf(page))
        found = re.fullmatch(rf"Books \((\d+) of {total}\)", text)
        assert found is not None, text
        assert int(found[1]) < total

    qtbot.waitUntil(labelled, timeout=5000)
