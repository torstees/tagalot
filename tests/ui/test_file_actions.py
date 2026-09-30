"""Open file, Show in file manager, and the Open with menu (#104), with per-user overrides
(#105)."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QMenu
from pytestqt.qtbot import QtBot
from sqlalchemy import update

from tagalot.core.handlers import Command, FileToOpen
from tagalot.core.models import Resource, ResourceStatus
from tagalot.core.search import SearchHit
from tagalot.core.session import KeepSession
from tagalot.core.settings import load_settings
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.file_actions import OpenWithMenu
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


def _open_with(qtbot: QtBot, menu: QMenu) -> OpenWithMenu:
    """The Open with submenu, filled (as when it is shown)."""
    submenu = next(a.menu() for a in menu.actions() if a.text() == "Open with")
    assert isinstance(submenu, OpenWithMenu)
    submenu.fill()
    qtbot.waitUntil(lambda: not _labels(submenu)[0].startswith("Looking"), timeout=5000)
    return submenu


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
    assert _labels(menu)[2:5] == ["Open file", "Show in file manager", "Open with"]
    _action(menu, "Open file")
    _action(menu, "Show in file manager")
    submenu = _open_with(qtbot, menu)
    assert _labels(submenu) == ["Choose a program…", "Always open .mp3 files with…"]
    _action(submenu, "Choose a program…")
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
    assert "Open with" not in _labels(menu)
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
    assert _labels(more)[:3] == ["Open file", "Show in file manager", "Open with"]
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


# --- per-user overrides (#105) ---


@pytest.fixture
def started(window: MainWindow, tmp_path: Path) -> list[Command]:
    """Commands the window started, instead of starting them; settings go to tmp_path."""
    calls: list[Command] = []
    window.files.start = calls.append  # type: ignore[method-assign, assignment]
    window.files.settings_path = tmp_path / "settings.toml"
    return calls


def test_always_open_a_kind_of_file_with_a_program(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    opened: list[tuple[str, FileToOpen]],
    started: list[Command],
    tmp_path: Path,
) -> None:
    program = str(tmp_path / "Tools" / "My Player.exe")
    window.files.pick_program = lambda title: program  # type: ignore[method-assign]
    page = _search(qtbot, window, SONGS, "12 items")
    _action(
        _open_with(qtbot, page.item_menu(_hit(qtbot, page, "Blue"))),
        "Always open .mp3 files with\u2026",
    )
    assert [(h.ext, h.command) for h in session.settings.handlers] == [
        (".mp3", f'"{program}" "{{path}}"')
    ]
    assert "Open file now uses My Player for .mp3 files" in window.statusBar().currentMessage()
    qtbot.waitUntil(lambda: (tmp_path / "settings.toml").exists(), timeout=5000)
    assert len(load_settings(tmp_path / "settings.toml").handlers) == 1  # saved

    # Open file now starts the program with the file, whatever the song.
    _action(page.item_menu(_hit(qtbot, page, "Take Five")), "Open file")
    qtbot.waitUntil(lambda: len(started) == 1, timeout=5000)
    assert started[0].program == program
    assert Path(started[0].args[0]).name == "01 Take Five.mp3"
    assert opened == []  # not the OS default

    # The menu offers to stop; then the OS default is back.
    submenu = _open_with(qtbot, page.item_menu(_hit(qtbot, page, "Blue")))
    assert _labels(submenu)[-1] == "Stop using My Player for .mp3 files"
    _action(submenu, "Stop using My Player for .mp3 files")
    assert session.settings.handlers == []
    _action(page.item_menu(_hit(qtbot, page, "Take Five")), "Open file")
    qtbot.waitUntil(lambda: len(opened) == 1, timeout=5000)
    assert len(started) == 1


def test_a_rule_for_a_role_and_a_template_from_settings(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    opened: list[tuple[str, FileToOpen]],
    started: list[Command],
) -> None:
    session.settings.set_handler(".flac", 'tool --folder "{dir}" --name {name}', role="audio")
    session.settings.set_handler(".flac", "other {path}", role="cover")  # another role: unused
    detail = _open(qtbot, window, session, "So What")
    more = detail.more_button.menu()
    _action(more, "Open file")  # the FLAC, linked as audio
    qtbot.waitUntil(lambda: len(started) == 1, timeout=5000)
    command = started[0]
    assert command.program == "tool"
    assert command.args[0] == "--folder"
    assert Path(command.args[1]).name == "Kind of Blue"
    assert command.args[2:] == ("--name", "01 So What.flac")


def test_a_broken_template_is_reported(
    qtbot: QtBot, window: MainWindow, session: KeepSession, started: list[Command]
) -> None:
    session.settings.set_handler(".mp3", "   ")
    page = _search(qtbot, window, SONGS, "12 items")
    _action(page.item_menu(_hit(qtbot, page, "Blue")), "Open file")
    qtbot.waitUntil(
        lambda: "command for .mp3 files doesn't work" in window.statusBar().currentMessage(),
        timeout=5000,
    )
    assert started == []
