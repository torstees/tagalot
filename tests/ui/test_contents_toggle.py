"""The In documents toggle in the filter bar (#350): offered when the keep searches inside
documents, it makes the search box find items by what their files say, and shows where
(#351)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from pytestqt.qtbot import QtBot

from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.search import Snippet
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.main_window import MainWindow
from tagalot.ui.models.results import MATCH, MATCH_HTML_ROLE, match_value
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import ScanController
from tests.core.book_files import paged_pdf
from tests.core.test_contents import THEME

pytestmark = pytest.mark.gui


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    themes, files = tmp_path / "themes", tmp_path / "files"
    themes.mkdir()
    files.mkdir()
    (themes / "docs.py").write_text(THEME, encoding="utf-8")
    (files / "botany.pdf").write_bytes(paged_pdf([["Leaves"], ["Photosynthesis needs light"]]))
    (files / "kitchen.txt").write_text("sugar in the cafe", encoding="utf-8")
    keep = create_keep(
        tmp_path / "Docs.keep", "Docs", ThemeRef("docs", 1), [RootConfig("f", "F", str(files))]
    )
    with KeepSession.open(keep.dir, Settings(theme_dirs=[themes])) as opened:
        yield opened


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.show()
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    return window


def _page(window: MainWindow) -> SearchPage:
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    return page


def test_contents_is_offered_only_when_the_keep_searches_documents(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page(window)
    assert page.filter_bar.contents_box.text() == "In documents"
    assert page.filter_bar.contents_box.isHidden()
    session.set_contents_index("words")
    with qtbot.waitSignal(window._contents_done, timeout=10_000):
        window.update_contents()  # as the Keep tab does
    assert not page.filter_bar.contents_box.isHidden()
    session.set_contents_index(None)
    window.update_contents()
    assert page.filter_bar.contents_box.isHidden()


def test_the_toggle_finds_items_by_their_contents(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    session.set_contents_index("words")
    with qtbot.waitSignal(window._contents_done, timeout=10_000):
        window.update_contents()
    page = _page(window)
    page.filter_bar.set_text("photosynth")
    qtbot.waitUntil(lambda: page.status.text() == "Nothing found", timeout=10_000)
    page.filter_bar.contents_box.setChecked(True)
    qtbot.waitUntil(lambda: page.status.text() == "1 item", timeout=5000)
    assert page.current_spec().contents is True
    saved = page.saved_definition()
    assert saved.filters.contents is True  # kept with a saved search


def test_where_each_item_matched(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    session.set_contents_index("words")
    with qtbot.waitSignal(window._contents_done, timeout=10_000):
        window.update_contents()
    page = _page(window)
    page.filter_bar.contents_box.setChecked(True)
    page.filter_bar.set_text("photosynth")
    qtbot.waitUntil(lambda: page.status.text() == "1 item", timeout=5000)
    keys = [c.key for c in page.model.columns]
    assert keys[:2] == ["title", MATCH]  # beside the title
    cell = page.model.index(0, keys.index(MATCH))
    qtbot.waitUntil(lambda: bool(cell.data()), timeout=5000)
    assert cell.data() == "Photosynthesis needs light"
    assert cell.data(MATCH_HTML_ROLE) == "<b>Photosynthesis</b> needs light"
    assert cell.data(Qt.ItemDataRole.ToolTipRole).endswith("botany.pdf, p. 2")
    assert page.grid.show_match  # cards end with it too

    page.filter_bar.contents_box.setChecked(False)  # no longer inside documents
    page.filter_bar.set_text("botany")  # by its title
    qtbot.waitUntil(lambda: MATCH not in [c.key for c in page.model.columns], timeout=5000)
    assert not page.grid.show_match


def test_naming_the_page() -> None:
    assert match_value(Snippet("Books/Guards.epub", 3, 30, "a")).where == "Guards.epub, ch. 3"
    assert match_value(Snippet("Books/Guards.AZW3", 3, 30, "a")).where == "Guards.AZW3, ch. 3"
    assert match_value(Snippet("p.pdf", 8, 12, "a")).where == "p.pdf, p. 8"
    assert match_value(Snippet("notes.txt", 1, 1, "a")).where == "notes.txt"
    value = match_value(Snippet("p.pdf", 1, 2, "a < \x02b\x03"))
    assert (value.text, value.html) == ("a < b", "a &lt; <b>b</b>")
    assert value.brief == "\u2026b"  # a card's line starts at the match
    assert match_value(Snippet("p.pdf", 1, 2, "\x02a\x03 b")).brief == "a b"
    inside = match_value(Snippet("p.pdf", 1, 2, "drops r\x02ecurr\x03ence now"))
    assert inside.brief == "\u2026recurrence now"  # from the start of the matched word


def test_offered_only_where_documents_can_be_listed(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    """Not on a page of authors or artists (a related section, a view of them): only on
    Search all and pages whose types include a document type."""
    from tagalot.core.search_spec import SearchSpec

    session.set_contents_index("words")
    pages = {
        types: SearchPage(session, "Page", SearchSpec(types=types))
        for types in [(), ("docs.doc",), ("docs.note",), ("docs.doc", "docs.note")]
    }
    for search in pages.values():
        qtbot.addWidget(search)
    assert {types: p.offers_contents() for types, p in pages.items()} == {
        (): True,
        ("docs.doc",): True,
        ("docs.note",): False,
        ("docs.doc", "docs.note"): True,
    }
    assert pages[("docs.note",)].filter_bar.contents_box.isHidden()
