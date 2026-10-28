"""Choosing your own icons (#557, DESIGN.md §12 *Icons*): the icon picker, a type's icon
from its search's menu, a saved search's, and the keep's in Keep configuration."""

import importlib.util
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QIcon
from pytestqt.qtbot import QtBot

from tagalot.core.saved_searches import SavedDefinition
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.icon_picker import IconChoice, IconPicker
from tagalot.ui.icons import icon, lucide
from tagalot.ui.keep_config import KeepConfigWindow
from tagalot.ui.main_window import MainWindow
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M3 3h18"/></svg>'


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


def _nav_icon(window: MainWindow, section: int, label: str) -> int:
    model = window.navigation.model()
    heading = model.index(section, 0)
    for row in range(model.rowCount(heading)):
        index = model.index(row, 0, heading)
        if index.data() == label:
            found = index.data(1)
            assert isinstance(found, QIcon)
            return found.cacheKey()
    raise AssertionError(f"no {label!r}")


def _toml(session: KeepSession) -> dict[str, object]:
    return tomllib.loads(session.keep.toml_path.read_text(encoding="utf-8"))


# --- the picker ---


def test_the_picker_searches_and_picks(qtbot: QtBot) -> None:
    picker = IconPicker("Icon for Albums", current="disc")
    qtbot.addWidget(picker)
    assert picker.selected_name() == "disc"  # the current one is selected
    assert picker.standard is None
    picker.search.setText("movie")
    assert {"film", "clapperboard"} <= set(picker.names())
    assert picker.selected_name() == picker.names()[0]  # Enter takes the first match
    picker.search.setText("no such icon anywhere")
    assert picker.names() == []
    assert picker.none_found.isVisibleTo(picker)
    assert not picker.ok.isEnabled()
    picker.search.setText("film")
    picker.grid.setCurrentIndex(picker.model.index(picker.names().index("film"), 0))
    picker.accept()
    assert picker.choice == IconChoice("film")


def test_the_picker_takes_a_file_or_the_standard_icon(qtbot: QtBot, tmp_path: Path) -> None:
    picker = IconPicker("Icon", standard="Use the theme's icon")
    qtbot.addWidget(picker)
    assert picker.standard is not None
    picker.ask_picture = lambda: tmp_path / "reel.svg"  # type: ignore[method-assign]
    picker.from_file.click()
    assert picker.choice == IconChoice(picture=tmp_path / "reel.svg")
    other = IconPicker("Icon", standard="Use the theme's icon")
    qtbot.addWidget(other)
    other.standard.click()  # type: ignore[union-attr]
    assert other.choice == IconChoice()


# --- a type's icon ---


def test_a_types_icon(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, str | None, str | None]] = []

    def choose(title: str, current: str | None, standard: str | None) -> IconChoice:
        asked.append((title, current, standard))
        return IconChoice("clapperboard")

    monkeypatch.setattr(window, "choose_icon", choose)
    assert _nav_icon(window, 1, "Albums") == lucide("disc").cacheKey()
    window.change_type_icon("music.album")
    assert asked == [("Icon for Albums", "disc", None)]
    qtbot.waitUntil(
        lambda: _nav_icon(window, 1, "Albums") == lucide("clapperboard").cacheKey(), timeout=5000
    )
    assert _toml(session)["appearance"] == {"icons": {"music.album": "clapperboard"}}
    assert session.thumbnails.type_icon("music.album") == "clapperboard"  # placeholders too
    assert window.statusBar().currentMessage() == "Changed the icon of Albums."

    # The theme's again.
    window.set_type_icon("music.album", IconChoice())
    qtbot.waitUntil(
        lambda: _nav_icon(window, 1, "Albums") == lucide("disc").cacheKey(), timeout=5000
    )
    assert "appearance" not in _toml(session)


def test_a_types_icon_from_a_file(
    qtbot: QtBot, window: MainWindow, session: KeepSession, tmp_path: Path
) -> None:
    picture = tmp_path / "vinyl.svg"
    picture.write_text(SVG, encoding="utf-8")
    window.set_type_icon("music.album", IconChoice(picture=picture))
    copied = session.keep.dir / "icons" / "vinyl.svg"
    qtbot.waitUntil(
        lambda: _nav_icon(window, 1, "Albums") == icon(str(copied)).cacheKey(), timeout=5000
    )
    assert copied.read_text(encoding="utf-8") == SVG
    assert _toml(session)["appearance"] == {"icons": {"music.album": "icons/vinyl.svg"}}


# --- a saved search's icon ---


def test_a_saved_searchs_icon(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    window.tag_actions.save_search("Jazz", SavedDefinition(), None)
    qtbot.waitUntil(lambda: bool(window._saved), timeout=5000)
    saved_id = next(iter(window._saved))
    standard = lucide("bookmark").cacheKey()
    assert _nav_icon(window, 2, "Jazz") == standard
    monkeypatch.setattr(window, "choose_icon", lambda *_: IconChoice("music-2"))
    window.change_saved_icon(saved_id)
    qtbot.waitUntil(
        lambda: _nav_icon(window, 2, "Jazz") == lucide("music-2").cacheKey(), timeout=5000
    )
    window.undo_action.trigger()  # one undo step
    qtbot.waitUntil(lambda: _nav_icon(window, 2, "Jazz") == standard, timeout=5000)


# --- the keep's icon ---


def test_the_keeps_icon(
    qtbot: QtBot, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = KeepConfigWindow(session)
    qtbot.addWidget(config)
    assert config.keep_icon.icon().cacheKey() == lucide("headphones").cacheKey()
    assert not config.keep_icon_theme.isEnabled()
    monkeypatch.setattr(config, "choose_icon", lambda *_: IconChoice("radio"))
    config.change_keep_icon()
    qtbot.waitUntil(lambda: config.keep_icon_theme.isEnabled(), timeout=5000)
    assert config.keep_icon.icon().cacheKey() == lucide("radio").cacheKey()
    assert _toml(session)["appearance"] == {"icon": "radio"}
    assert session.keep_icon() == "radio"
    config.keep_icon_theme.click()
    qtbot.waitUntil(lambda: not config.keep_icon_theme.isEnabled(), timeout=5000)
    assert session.keep_icon() == "headphones"
