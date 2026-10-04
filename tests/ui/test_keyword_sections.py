"""File keywords on an item's page, in the tagging panel, and in the tag manager (#295), on
the music demo: "So What" says it is Jazz, which no tag matches until it is mapped."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QLabel, QPushButton, QWidget
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import PATH_SEPARATOR
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.tag_manager import TagManagerPage
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

ARROW = chr(0x2192)
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_music_demo(tmp_path), Settings()) as session:
        yield session


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()
    qtbot.waitUntil(lambda: window.tag_panel.loaded, timeout=5000)
    return window


def _id(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None
    return found


def _tag(session: KeepSession, name: str) -> int:
    tree = session.tag_cache.get()
    [tag_id] = [t for t in tree if tree.node(t).name == name]
    return tag_id


def _page(qtbot: QtBot, window: MainWindow, entity_id: int) -> DetailPage:
    window.open_entity(entity_id)
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: page.detail is not None, timeout=5000)
    return page


def _state(page: DetailPage, key: str) -> str:
    label = page.findChild(QLabel, f"keyword_{key}")
    return label.text() if label is not None else ""


def _button(page: DetailPage, text: str) -> QPushButton:
    box = page.findChild(QWidget, "section_keywords")
    assert box is not None
    [button] = [b for b in box.findChildren(QPushButton) if b.text() == text]
    return button


def _status(qtbot: QtBot, window: MainWindow, expected: str) -> None:
    def check() -> None:
        assert window.statusBar().currentMessage() == expected

    qtbot.waitUntil(check, timeout=5000)


def test_the_page_says_what_the_file_says_and_maps_it(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calm = _tag(session, "Calm")
    monkeypatch.setattr(window, "choose_keyword_tag", lambda dialog: calm)
    page = _page(qtbot, window, _id(session, "So What"))
    assert _state(page, "jazz") == "No tag"
    _button(page, "Map…").click()
    _status(qtbot, window, f"Mapped 'Jazz' to Mood{PATH_SEPARATOR}Calm.")
    qtbot.waitUntil(
        lambda: _state(page, "jazz") == f"{ARROW} Mood{PATH_SEPARATOR}Calm", timeout=5000
    )


def test_a_removed_file_tag_can_be_restored(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    calm, so_what = _tag(session, "Calm"), _id(session, "So What")
    session.tags.map_keywords(calm, ["Jazz"])
    session.tags.remove([so_what], [calm])
    page = _page(qtbot, window, so_what)
    assert _state(page, "jazz") == f"{ARROW} Mood{PATH_SEPARATOR}Calm (removed from this item)"
    _button(page, "Restore").click()
    _status(qtbot, window, "Restored 'Calm': the item's file gives it again.")
    qtbot.waitUntil(
        lambda: _state(page, "jazz") == f"{ARROW} Mood{PATH_SEPARATOR}Calm", timeout=5000
    )


def test_ignoring_from_the_page(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _page(qtbot, window, _id(session, "So What"))
    _button(page, "Ignore").click()
    _status(qtbot, window, "Ignoring 1 keyword.")
    qtbot.waitUntil(lambda: _state(page, "jazz") == "Ignored", timeout=5000)
    _button(page, "Stop ignoring").click()
    qtbot.waitUntil(lambda: _state(page, "jazz") == "No tag", timeout=5000)


def test_tags_from_files_are_italic_in_the_panel(window: MainWindow, session: KeepSession) -> None:
    calm, loud = _tag(session, "Calm"), _tag(session, "Loud")
    model = window.tag_panel.model
    window.tag_panel.set_selection(2, {calm: 2, loud: 1}, frozenset(), {calm: 2})
    font = model.data(model.index_of(calm), Qt.ItemDataRole.FontRole)
    assert font is not None
    assert font.italic()
    assert model.data(model.index_of(loud), Qt.ItemDataRole.FontRole) is None
    assert "From the files of 2" in model.data(model.index_of(calm), Qt.ItemDataRole.ToolTipRole)


def test_the_tag_manager_counts_uses_from_files(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    calm = _tag(session, "Calm")
    session.tags.map_keywords(calm, ["Jazz"])
    window.navigation.select(NavTarget("tags", label="Tag manager"))
    page = window.stack.currentWidget()
    assert isinstance(page, TagManagerPage)
    qtbot.waitUntil(lambda: page.loaded, timeout=5000)
    assert page.select_tag(calm)
    # Calm is on 3 songs by hand; Jazz gives it to the 3 other Jazz songs (Blue and Blue in
    # Green already had it).
    qtbot.waitUntil(lambda: "from their files" in page.details.counts.text(), timeout=5000)
    assert page.details.counts.text().startswith("6 items directly (3 from their files)")
