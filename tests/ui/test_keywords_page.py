"""TOOLS → File keywords (#295), on the music demo: its songs' genres (Jazz, Blues, Rock)
are file keywords no tag matches yet."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, QThreadPool
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import EntityTag
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import PATH_SEPARATOR
from tagalot.ui.keywords_page import KeywordsPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.tag_picker import TagPickerDialog
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
KEYWORDS = NavTarget("keywords", label="File keywords")


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
    window.resize(1200, 700)
    window.show()
    window.navigation.select(KEYWORDS)
    qtbot.waitUntil(lambda: _page(window).loaded, timeout=5000)
    return window


def _page(window: MainWindow) -> KeywordsPage:
    page = window.stack.currentWidget()
    assert isinstance(page, KeywordsPage)
    return page


def _listed(page: KeywordsPage) -> list[tuple[str, str, str]]:
    return [(info.keyword, f"{info.items}", page.model.tag_text(info)) for info in page.model.rows]


def _select(page: KeywordsPage, keyword: str) -> None:
    [row] = [r for r, info in enumerate(page.model.rows) if info.keyword == keyword]
    page.table.selectionModel().select(
        page.model.index(row, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )


def _nav_text(window: MainWindow) -> str:
    return str(window.navigation.index_of(KEYWORDS).data())


def _tag(session: KeepSession, name: str) -> int:
    tree = session.tag_cache.get()
    [tag_id] = [t for t in tree if tree.node(t).name == name]
    return tag_id


def _file_tagged(session: KeepSession, tag_id: int) -> int:
    with session.reader.connect() as conn:
        rows = conn.execute(
            select(EntityTag.entity_id).where(EntityTag.tag_id == tag_id, EntityTag.by_file)
        )
        return len(rows.all())


def _status(qtbot: QtBot, window: MainWindow, expected: str) -> None:
    def check() -> None:
        assert window.statusBar().currentMessage() == expected

    qtbot.waitUntil(check, timeout=5000)


def test_unmatched_genres_are_listed_and_counted(qtbot: QtBot, window: MainWindow) -> None:
    page = _page(window)
    assert _listed(page) == [("Jazz", "5", "—"), ("Rock", "3", "—"), ("Blues", "2", "—")]
    assert page.status.text() == "3 don't match a tag"
    qtbot.waitUntil(lambda: _nav_text(window) == "File keywords (3)", timeout=5000)
    page.show_box.setCurrentText("Matched")
    assert _listed(page) == []


def test_mapping_a_keyword_tags_every_item_and_undoes(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    favorites = _tag(session, "Favorites")
    titles: list[str] = []

    def pick(dialog: TagPickerDialog) -> int:
        titles.append(dialog.windowTitle())
        return favorites

    monkeypatch.setattr(window, "choose_keyword_tag", pick)
    page = _page(window)
    _select(page, "Jazz")
    page.map_button.click()
    _status(qtbot, window, "Mapped 'Jazz' to Favorites.")
    assert titles == ["Map “Jazz”"]
    assert _file_tagged(session, favorites) == 5 - 1  # So What already had it, by hand
    qtbot.waitUntil(lambda: _nav_text(window) == "File keywords (2)", timeout=5000)
    qtbot.waitUntil(lambda: [k for k, _, _ in _listed(page)] == ["Rock", "Blues"], timeout=5000)

    window.tag_actions.undo()
    _status(qtbot, window, "Undid: Map 'Jazz' to 'Favorites'.")
    assert _file_tagged(session, favorites) == 0
    qtbot.waitUntil(lambda: _nav_text(window) == "File keywords (3)", timeout=5000)


def test_creating_a_tag_for_a_keyword(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, str]] = []

    def ask(keyword: str, default: str) -> str:
        asked.append((keyword, default))
        return "Genre > Rock"

    monkeypatch.setattr(window, "ask_keyword_tag_path", ask)
    page = _page(window)
    _select(page, "Rock")
    page.create_button.click()
    _status(qtbot, window, f"Created tag Genre{PATH_SEPARATOR}Rock for 'Rock'.")
    assert asked == [("Rock", "Rock")]
    rock = _tag(session, "Rock")
    assert _file_tagged(session, rock) == 3  # its name matches: no alias needed
    assert session.tag_cache.get().aliases(rock) == ()


def test_ignoring_a_keyword(qtbot: QtBot, window: MainWindow) -> None:
    page = _page(window)
    _select(page, "Blues")
    assert page.ignore_button.text() == "Ignore"
    page.ignore_button.click()
    _status(qtbot, window, "Ignoring 1 keyword.")
    qtbot.waitUntil(lambda: _nav_text(window) == "File keywords (2)", timeout=5000)
    page.show_box.setCurrentText("Ignored")
    qtbot.waitUntil(lambda: _listed(page) == [("Blues", "2", "Ignored")], timeout=5000)
    _select(page, "Blues")
    assert page.ignore_button.text() == "Stop ignoring"
    page.ignore_button.click()
    _status(qtbot, window, "No longer ignoring 1 keyword.")
    qtbot.waitUntil(lambda: _nav_text(window) == "File keywords (3)", timeout=5000)


def test_a_scan_reports_new_keywords(qtbot: QtBot, window: MainWindow) -> None:
    window.activity.set_new_keywords(2)
    assert window.activity.keywords.isVisibleTo(window.activity)
    assert "2 new file keywords don't match a tag." in window.activity.keywords.text()
    window.navigation.select(NavTarget("search", label="Search all"))
    window.activity.review_keywords.emit()
    assert isinstance(window.stack.currentWidget(), KeywordsPage)


def test_a_new_tag_named_otherwise_gets_the_keyword_as_its_alias(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        window, "ask_keyword_tag_path", lambda keyword, default: "Genre > Jazz music"
    )
    page = _page(window)
    _select(page, "Jazz")
    page.create_button.click()
    qtbot.waitUntil(
        lambda: (
            "Jazz music" in [session.tag_cache.get().node(t).name for t in session.tag_cache.get()]
        ),
        timeout=5000,
    )
    jazz = _tag(session, "Jazz music")
    qtbot.waitUntil(lambda: _file_tagged(session, jazz) == 5, timeout=5000)
    assert session.tag_cache.get().aliases(jazz) == ("Jazz",)
