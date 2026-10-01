"""The music theme's views in the window: Browse as a tree, songs in track order (#101)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QPushButton
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.workers import ScanController
from tests.ui.test_contents_search import _open
from tests.ui.test_tree_toggles import _tree_row, _tree_titles
from tests.ui.test_within import _search

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
BROWSE = NavTarget("view", key="Browse", label="Browse")


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
def window(qtbot: QtBot, session: KeepSession) -> Iterator[MainWindow]:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()
    yield window
    window.thumbnails.clear()
    window.thumbnails.wait()


def test_browse_is_a_tree_of_albums_with_their_songs_in_order(
    qtbot: QtBot, window: MainWindow
) -> None:
    page = _search(qtbot, window, BROWSE, "6 items")  # five albums and a song with none
    assert page.results.currentWidget() is page.tree
    assert page.filter_bar.inherit_box.isChecked()
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    assert sorted(_tree_titles(page)) == [
        "Demo Tape", "Jazz Hits", "Kind of Blue", "Live Box", "Pastel Blues", "Untagged",
    ]  # fmt: skip

    blue = _tree_row(page, "Kind of Blue")
    page.tree.expand(blue)
    qtbot.waitUntil(lambda: page.tree_model.rowCount(blue) == 3, timeout=5000)
    assert _tree_titles(page, blue) == ["So What", "Freddie Freeloader", "Blue in Green"]
    live = _tree_row(page, "Live Box")
    page.tree.expand(live)
    qtbot.waitUntil(lambda: page.tree_model.rowCount(live) == 3, timeout=5000)
    assert _tree_titles(page, live) == ["Intro", "Tie Your Mother Down", "Intro"]  # by disc

    # As a list, Contained lists the albums' songs too, each once.
    page.list_button.click()
    qtbot.waitUntil(lambda: page.status.text() == "17 items", timeout=5000)


def test_an_album_page_lists_its_songs_in_track_order(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    detail = _open(qtbot, window, session, "Kind of Blue")
    contents = detail.contents
    assert contents is not None
    qtbot.waitUntil(lambda: contents.status.text() == "3 items", timeout=5000)
    qtbot.waitUntil(lambda: contents.model.hit(2) is not None, timeout=5000)
    hits = [contents.model.hit(r) for r in range(3)]
    assert [h.title for h in hits if h] == ["So What", "Freddie Freeloader", "Blue in Green"]


def test_play_album_from_its_page(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    nothing_is_launched: list[tuple[str, str]],
) -> None:
    detail = _open(qtbot, window, session, "Kind of Blue")
    play = detail.findChild(QPushButton, "action_play_album")
    assert isinstance(play, QPushButton)
    assert play.text() == "Play album"
    play.click()
    qtbot.waitUntil(lambda: len(nothing_is_launched) == 1, timeout=5000)
    how, path = nothing_is_launched[0]
    assert (how, Path(path).name) == ("open", "Kind of Blue.m3u8")
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    assert [Path(p).name for p in lines[2::2]] == [
        "01 So What.flac",
        "02 Freddie Freeloader.mp3",
        "03 Blue in Green.mp3",
    ]
    assert window.statusBar().currentMessage() == "Playing 3 songs from Kind of Blue."
    assert window.undo_action.text() == "Undo"  # playing changes nothing
