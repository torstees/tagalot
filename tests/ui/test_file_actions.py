"""Open file, Show in file manager, and Open with… in menus (#104)."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QMenu
from pytestqt.qtbot import QtBot
from sqlalchemy import update

from tagalot.core.handlers import FileToOpen
from tagalot.core.models import Resource, ResourceStatus
from tagalot.core.search import SearchHit
from tagalot.core.session import KeepSession
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tests.ui.test_contents_search import _open
from tests.ui.test_music_views import session, window
from tests.ui.test_within import _search

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

SONGS = NavTarget("view", key="Songs", label="Songs")
ALBUMS = NavTarget("view", key="Albums", label="Albums")
ARTISTS = NavTarget("view", key="Artists", label="Artists")


def _labels(menu: QMenu) -> list[str]:
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def _action(menu: QMenu, text: str) -> None:
    next(a for a in menu.actions() if a.text() == text).trigger()


def _hit(qtbot: QtBot, page: SearchPage, title: str) -> SearchHit:
    qtbot.waitUntil(lambda: page.model.hit(page.model.rowCount() - 1) is not None, timeout=5000)
    hits = [page.model.hit(r) for r in range(page.model.rowCount())]
    return next(h for h in hits if h is not None and h.title == title)


@pytest.fixture
def opened(window: MainWindow) -> list[tuple[str, FileToOpen]]:
    """What the window was asked to open, instead of starting programs."""
    calls: list[tuple[str, FileToOpen]] = []
    window.files.open = lambda file: calls.append(("open", file))  # type: ignore[method-assign]
    window.files.reveal = lambda file: calls.append(("reveal", file))  # type: ignore[method-assign]
    window.files.open_with = lambda file: calls.append(("open_with", file))  # type: ignore[method-assign]
    return calls


def test_a_songs_menu_opens_its_file(
    qtbot: QtBot, window: MainWindow, opened: list[tuple[str, FileToOpen]]
) -> None:
    page = _search(qtbot, window, SONGS, "12 items")
    menu = page.item_menu(_hit(qtbot, page, "Freddie Freeloader"))
    assert _labels(menu)[2:5] == ["Open file", "Show in file manager", "Open with…"]
    for text in ("Open file", "Show in file manager", "Open with…"):
        _action(menu, text)
    qtbot.waitUntil(lambda: len(opened) == 3, timeout=5000)
    assert sorted(how for how, _ in opened) == ["open", "open_with", "reveal"]  # workers
    path = Path(opened[0][1].path)
    assert (path.name, path.is_file()) == ("02 Freddie Freeloader.mp3", True)


def test_an_album_opens_its_folder_and_an_artist_has_nothing_to_open(
    qtbot: QtBot, window: MainWindow, opened: list[tuple[str, FileToOpen]]
) -> None:
    albums = _search(qtbot, window, ALBUMS, "5 items")
    menu = albums.item_menu(_hit(qtbot, albums, "Kind of Blue"))
    assert "Open folder" in _labels(menu)
    assert "Open with…" not in _labels(menu)
    _action(menu, "Open folder")
    qtbot.waitUntil(lambda: len(opened) == 1, timeout=5000)
    assert opened[0][1].is_dir
    assert Path(opened[0][1].path).name == "Kind of Blue"

    artists = _search(qtbot, window, ARTISTS, "7 items")
    labels = _labels(artists.item_menu(_hit(qtbot, artists, "Miles Davis")))
    assert "Open file" not in labels
    assert "Open folder" not in labels


def test_a_missing_file_says_so(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    opened: list[tuple[str, FileToOpen]],
) -> None:
    session.writer.run(
        lambda conn: conn.execute(
            update(Resource)
            .where(Resource.relpath.endswith("02 Freddie Freeloader.mp3"))
            .values(status=ResourceStatus.MISSING)
        )
    )
    page = _search(qtbot, window, SONGS, "12 items")
    _action(page.item_menu(_hit(qtbot, page, "Freddie Freeloader")), "Open file")
    qtbot.waitUntil(
        lambda: "missing at the last scan" in window.statusBar().currentMessage(), timeout=5000
    )
    assert opened == []


def test_the_detail_page_opens_the_item_or_one_of_its_files(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    opened: list[tuple[str, FileToOpen]],
) -> None:
    detail = _open(qtbot, window, session, "So What")
    assert isinstance(detail, DetailPage)
    more = detail.more_button.menu()
    assert _labels(more)[:3] == ["Open file", "Show in file manager", "Open with…"]
    _action(more, "Open file")
    qtbot.waitUntil(lambda: len(opened) == 1, timeout=5000)
    assert Path(opened[0][1].path).suffix == ".flac"  # the first version

    # Each file row has its own menu: open the MP3 version.
    detail_files = [f for s in detail.detail.sections for f in s.files] if detail.detail else []
    mp3 = next(f for f in detail_files if f.relpath.endswith(".mp3"))
    _action(detail._file_menu(mp3), "Show in file manager")
    qtbot.waitUntil(lambda: len(opened) == 2, timeout=5000)
    assert opened[1][0] == "reveal"
    assert Path(opened[1][1].path).suffix == ".mp3"
