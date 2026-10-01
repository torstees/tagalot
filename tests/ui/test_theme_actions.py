"""Theme actions in menus and on detail pages, with undo (#106), on the media demo."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QPushButton
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.handlers import FileToOpen
from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.themes.loader import load_themes
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import ScanController
from tests.ui.test_contents_search import _id, _open
from tests.ui.test_file_actions import _action, _labels
from tests.ui.test_within import _hit, _search

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
SEARCH_ALL = NavTarget("search", label="Search all")


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    themes = tmp_path / "themes"
    keep_dir = module.make_media_demo(tmp_path, themes_dir=themes)
    catalog = load_themes(user_dir=themes)
    with KeepSession.open(keep_dir, Settings(), catalog=catalog) as session:
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


def _length(session: KeepSession, title: str) -> int:
    table = next(t for t in session.schema.entities.values() if t.type_id.endswith(".song"))
    with session.reader.connect() as conn:
        value = conn.scalar(
            select(table.table.c.length)
            .join(Entity, Entity.id == table.table.c.id)
            .where(Entity.title == title)
        )
    assert isinstance(value, int)
    return value


def test_a_page_button_runs_an_action_that_can_be_undone(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    detail = _open(qtbot, window, session, "Song 2")
    assert isinstance(detail, DetailPage)
    buttons = [
        b.text() for b in detail.findChildren(QPushButton) if b.objectName().startswith("action_")
    ]
    assert buttons == ["Double the length", "Save the titles"]
    double = detail.findChild(QPushButton, "action_double_length")
    assert isinstance(double, QPushButton)
    double.click()
    qtbot.waitUntil(lambda: _length(session, "Song 2") == 244, timeout=5000)
    qtbot.waitUntil(
        lambda: window.statusBar().currentMessage() == "Doubled the length of 1 songs.",
        timeout=5000,
    )
    assert window.undo_action.text() == "Undo Double the length"
    window.undo_action.trigger()
    qtbot.waitUntil(lambda: _length(session, "Song 2") == 122, timeout=5000)
    qtbot.waitUntil(window.redo_action.isEnabled, timeout=5000)
    assert window.redo_action.text() == "Redo Double the length"
    window.redo_action.trigger()
    qtbot.waitUntil(lambda: _length(session, "Song 2") == 244, timeout=5000)


def test_a_menu_action_writes_and_opens_a_file(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    opened: list[FileToOpen] = []
    window.files.open = opened.append  # type: ignore[method-assign, assignment]
    page = _search(qtbot, window, SEARCH_ALL, "22 items")
    qtbot.waitUntil(lambda: _found(page, "Abbey Road"), timeout=5000)
    menu = page.item_menu(_hit(page, "Abbey Road"))
    assert _labels(menu)[-1:] == ["Save the titles"]  # albums can't double a length
    _action(menu, "Save the titles")
    qtbot.waitUntil(lambda: len(opened) == 1, timeout=5000)
    path = Path(opened[0].path)
    assert path.parent == session.temp_dir()
    assert path.read_text(encoding="utf-8").splitlines() == ["Abbey Road"]
    assert window.statusBar().currentMessage() == "Saved 1 titles."
    assert window.undo_action.text() == "Undo"  # nothing changed, nothing to undo


def test_an_action_runs_on_the_selected_items_it_applies_to(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    window.run_action("double_length", [_id(session, "Song 2"), _id(session, "Parklife")])
    qtbot.waitUntil(lambda: _length(session, "Song 2") == 244, timeout=5000)
    assert _length(session, "Come Together") == 259


def test_closing_the_keep_deletes_the_temp_folder(session: KeepSession) -> None:
    folder = session.temp_dir()
    (folder / "x.txt").write_text("x", encoding="utf-8")
    session.close()
    assert not folder.exists()


def _found(page: SearchPage, title: str) -> bool:
    try:
        _hit(page, title)
    except AssertionError:
        return False
    return True
