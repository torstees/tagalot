"""A container's detail page embeds a search of its contents (#87)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, QThreadPool
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import entity_tags
from tagalot.core.ui_state import load_ui_state
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
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as session:
        yield session


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> Iterator[MainWindow]:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()
    yield window
    window.thumbnails.clear()
    window.thumbnails.wait()


def _id(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None, title
    return found


def _open(qtbot: QtBot, window: MainWindow, session: KeepSession, title: str) -> DetailPage:
    window.open_entity(_id(session, title))
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: page.detail is not None, timeout=5000)
    return page


def _contents(qtbot: QtBot, page: DetailPage, count: str) -> SearchPage:
    contents = page.contents
    assert contents is not None
    qtbot.waitUntil(lambda: contents.status.text() == count, timeout=5000)
    return contents


def _titles(contents: SearchPage) -> list[str]:
    hits = [contents.model.hit(r) for r in range(contents.model.rowCount())]
    return sorted(h.title for h in hits if h is not None)


def test_an_artists_page_lists_its_assets(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _open(qtbot, window, session, "Aurora Studio")
    contents = _contents(qtbot, page, "7 items")
    assert _titles(contents) == [
        "Aileron-Regular.ttf",
        "UI pack.zip",
        "dusk sky.png",
        "folder.jpg",
        "forest.png",
        "gem.png",
        "heart.png",
    ]
    assert contents.results.currentWidget() is contents.grid  # contents start as a grid
    assert page.findChild(QWidget, "section_contents") is None  # no separate count
    assert contents in window.search_pages()  # refreshed with the others


def test_the_contents_have_their_own_filters(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    contents = _contents(qtbot, _open(qtbot, window, session, "Aurora Studio"), "7 items")
    contents.filter_bar.text_edit.setText("sky")
    contents.filter_bar.text_edit.returnPressed.emit()
    qtbot.waitUntil(lambda: contents.status.text() == "1 item", timeout=5000)
    assert _titles(contents) == ["dusk sky.png"]


def test_tagging_applies_to_selected_contents_else_the_page(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _open(qtbot, window, session, "Kenji Sato")
    contents = _contents(qtbot, page, "4 items")
    tree = session.tag_cache.get()
    favorites = tree.find_child(None, "Favorites")
    assert favorites is not None

    # Nothing selected: the artist is tagged.
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.tag_selection([favorites])
    kenji = _id(session, "Kenji Sato")
    with session.reader.connect() as conn:
        assert favorites in entity_tags(conn, [kenji]).get(kenji, set())

    # An asset selected: it is tagged, not the artist again.
    qtbot.waitUntil(lambda: contents.model.hit(0) is not None, timeout=5000)
    titles = [getattr(contents.model.hit(r), "title", None) for r in range(4)]
    row = titles.index("tileset.png")
    contents.table.selectionModel().select(
        contents.model.index(row, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    style = tree.find_child(None, "Style")
    sketch = tree.find_child(style, "Sketch")
    assert sketch is not None
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.tag_selection([sketch])
    tileset = _id(session, "tileset.png")
    with session.reader.connect() as conn:
        tagged = entity_tags(conn, [tileset, kenji])
    assert sketch in tagged[tileset]
    assert sketch not in tagged.get(kenji, set())


def test_opening_an_asset_from_the_contents(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    contents = _contents(qtbot, _open(qtbot, window, session, "Kenji Sato"), "4 items")
    qtbot.waitUntil(lambda: contents.model.hit(0) is not None, timeout=5000)
    with qtbot.waitSignal(contents.open_requested):
        contents.grid.activated.emit(contents.model.index(0, 0))
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    assert page.entity_id == contents.model.hit(0).id  # type: ignore[union-attr]
    qtbot.waitUntil(lambda: page.detail is not None, timeout=5000)
    assert page.contents is None  # an asset contains nothing


def test_the_contents_layout_is_remembered_per_type(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    contents = _contents(qtbot, _open(qtbot, window, session, "Aurora Studio"), "7 items")
    contents.list_button.click()
    assert load_ui_state(session.keep.ui_state_path)["layouts"] == {
        "contents:assets2d.artist": "list"
    }
    other = _contents(qtbot, _open(qtbot, window, session, "Kenji Sato"), "4 items")
    assert other.results.currentWidget() is other.table  # another artist, the same choice
