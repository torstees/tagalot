"""The Tags panel on an item's page (#504): it counts again after a scan, says whose tags
it shows, and goes back to the page's item from a selection in its contents. On the books
demo."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt, QThreadPool
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import Entity
from tagalot.core.online import LookupReport
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
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
    with KeepSession.open(module.make_books_demo(tmp_path), Settings()) as session:
        yield session


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()
    qtbot.waitUntil(lambda: window.tag_panel.loaded, timeout=5000)
    return window


def _author_page(qtbot: QtBot, window: MainWindow, name: str) -> DetailPage:
    session = window.session
    assert session is not None
    with session.reader.connect() as conn:
        author = conn.scalar(
            select(Entity.id).where(Entity.title == name, Entity.type == "books.author")
        )
    assert author is not None
    window.open_entity(author)
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: bool(page.embedded()) and page.detail is not None, timeout=5000)
    return page


def _shown(page: DetailPage) -> SearchPage:
    """The search shown below the page's header (an author's: the books they wrote)."""
    shown = page._tabs.currentWidget() if page._tabs is not None else page._bottom
    assert isinstance(shown, SearchPage)
    return shown


def _tag(session: KeepSession, name: str) -> int:
    tree = session.tag_cache.get()
    return next(t for t in tree if tree.node(t).name == name)


def test_the_panel_counts_again_after_a_scan(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    """Terry's note gains a tag; after Scan now, his page's checks show it."""
    page = _author_page(qtbot, window, "Terry Pratchett")
    read = _tag(session, "Read")
    panel = window.tag_panel
    qtbot.waitUntil(lambda: panel.model.check_state(read) is not None, timeout=5000)
    assert panel.model.check_state(read) == Qt.CheckState.Unchecked
    note = Path(session.root_path("books")) / "Notes" / "Authors" / "Terry Pratchett.md"
    note.write_text(note.read_text("utf-8").replace("---\n", "---\ntags: [Read]\n", 1), "utf-8")
    with qtbot.waitSignal(window.scans.finished, timeout=20_000):
        window.scan_now()
    qtbot.waitUntil(lambda: panel.model.check_state(read) == Qt.CheckState.Checked, timeout=5000)
    assert window.stack.currentWidget() is page  # still on his page


def test_the_panel_says_whose_tags_and_goes_back_to_the_pages_item(
    qtbot: QtBot, window: MainWindow
) -> None:
    page = _author_page(qtbot, window, "Terry Pratchett")
    label = window.tag_panel.selection_label
    qtbot.waitUntil(lambda: "Terry Pratchett</b>'s tags" in label.text(), timeout=5000)
    contents = _shown(page)
    qtbot.waitUntil(lambda: contents.model.hit(0) is not None, timeout=5000)
    contents.table.selectionModel().select(
        contents.model.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    qtbot.waitUntil(lambda: "1 item selected" in label.text(), timeout=5000)
    assert "Back to Terry Pratchett's tags" in label.text()
    label.linkActivated.emit("back")
    qtbot.waitUntil(lambda: "Terry Pratchett</b>'s tags" in label.text(), timeout=5000)
    assert not contents.table.selectionModel().hasSelection()


@pytest.mark.parametrize("finished", ["scan", "lookup"])
def test_scans_and_lookups_count_the_selections_tags_again(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, finished: str
) -> None:
    """Whatever the page: one with no search below its header gets no refresh that would
    count them as a side effect (#504)."""
    counted: list[str] = []
    monkeypatch.setattr(window, "_schedule_summary", lambda: counted.append("counted"))
    if window.lookups is not None:  # no lookup after the scan (it would ask first)
        monkeypatch.setattr(window.lookups, "after_scan", lambda since: None)
    if finished == "scan":
        window.scans.finished.emit([])
    else:
        window._lookups_finished(LookupReport(items=1), by_user=True)
    assert counted
