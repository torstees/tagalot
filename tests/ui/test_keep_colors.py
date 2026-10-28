"""A keep's colors in the window (#558, DESIGN.md §12 *Colors*): a whole palette from five
colors, applied with Fusion while the keep's window is active, chosen in Keep
configuration's Appearance tab; named colors for errors, warnings, and stars."""

import importlib.util
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QProxyStyle
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui import colors
from tagalot.ui.appearance import (
    apply_colors,
    custom_palette,
    hard_to_read,
    reset_colors,
    starting_colors,
)
from tagalot.ui.keep_config import KeepConfigWindow
from tagalot.ui.main_window import MainWindow
from tagalot.ui.result_table import ResultTable
from tagalot.ui.selection import contrast
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
OWN = {
    "background": "#1f1d1a",
    "text": "#ece6dc",
    "panels": "#2a2722",
    "accent": "#c8963e",
    "dimmed": "#9a9184",
}


@pytest.fixture(autouse=True)
def system_colors_after(qapp: QApplication) -> Iterator[None]:
    """Every test leaves the application with the system's colors."""
    window = qapp.palette().color(QPalette.ColorRole.Window).name()
    style = qapp.style().name()
    yield
    reset_colors()
    assert qapp.style().name() == style
    assert qapp.palette().color(QPalette.ColorRole.Window).name() == window


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_music_demo(tmp_path), Settings()) as session:
        yield session


def _window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1000, 700)
    window.show()
    return window


def _close(window: MainWindow) -> None:
    window.thumbnails.clear()
    window.thumbnails.wait()


def _toml(session: KeepSession) -> dict[str, object]:
    return tomllib.loads(session.keep.toml_path.read_text(encoding="utf-8"))


# --- the palette ---


def test_a_palette_from_five_colors() -> None:
    palette = custom_palette(OWN)
    role = QPalette.ColorRole

    def named(
        r: QPalette.ColorRole, group: QPalette.ColorGroup = QPalette.ColorGroup.Active
    ) -> str:
        return palette.color(group, r).name()

    assert named(role.Window) == OWN["background"]
    assert named(role.WindowText) == named(role.Text) == OWN["text"]
    assert named(role.Base) == OWN["panels"]
    assert named(role.Highlight) == named(role.Accent) == OWN["accent"]
    assert named(role.PlaceholderText) == OWN["dimmed"]
    assert named(role.AlternateBase) not in (OWN["panels"], OWN["text"])  # derived
    # Text on the accent is whichever of black or white reads best on it.
    assert named(role.HighlightedText) == "#000000"
    assert named(role.Link) == OWN["accent"]  # it reads on the panels
    disabled = named(role.Text, QPalette.ColorGroup.Disabled)
    assert disabled not in (OWN["text"], OWN["dimmed"])


def test_links_that_wouldnt_read_are_moved_toward_the_text() -> None:
    palette = custom_palette({**OWN, "accent": "#2c2a26"})  # nearly the panels' color
    link = palette.color(QPalette.ColorRole.Link)
    assert contrast(link, QColor(OWN["panels"])) > contrast(
        QColor("#2c2a26"), QColor(OWN["panels"])
    )


def test_hard_to_read_colors_are_named() -> None:
    assert hard_to_read(OWN) == []
    problems = hard_to_read({**OWN, "text": "#3a3630"})
    assert len(problems) == 2
    assert problems[0].startswith("Text on the background is hard to read (contrast 1.")
    assert "lists and panels" in problems[1]


def test_named_colors_suit_light_and_dark(qapp: QApplication) -> None:
    light = custom_palette({**OWN, "background": "#fbf8f2", "text": "#222222"})
    dark = custom_palette(OWN)
    assert colors.color(colors.ERROR, light) != colors.color(colors.ERROR, dark)
    for palette in (light, dark):
        ground = palette.color(QPalette.ColorRole.Window)
        assert contrast(colors.color(colors.ERROR, palette), ground) >= 4.5
        assert contrast(colors.color(colors.STAR, palette), ground) >= 2.0
    assert colors.color(colors.DIFFERS).alpha() < 255  # over a cell's text


# --- applying it ---


def test_applying_a_keeps_colors_and_back(qapp: QApplication) -> None:
    assert apply_colors("custom", OWN)
    assert qapp.style().name().lower() == "fusion"
    assert qapp.palette().color(QPalette.ColorRole.Window).name() == OWN["background"]
    assert not apply_colors("custom", OWN)  # the same look: nothing to do
    assert apply_colors("custom", {**OWN, "accent": "#3e7cc8"})
    assert qapp.palette().color(QPalette.ColorRole.Highlight).name() == "#3e7cc8"
    # (the fixture checks that the system's colors come back)


def test_results_lists_follow_the_style(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    try:
        tables = window.findChildren(ResultTable)
        assert tables
        apply_colors("custom", OWN)
        for table in tables:
            style = table.style()
            assert isinstance(style, QProxyStyle)
            base = style.baseStyle()
            assert base is not None
            assert base.name().lower() == "fusion"
    finally:
        _close(window)


def test_the_window_shows_its_keeps_colors(
    qtbot: QtBot, qapp: QApplication, session: KeepSession
) -> None:
    session.set_colors("custom", OWN)
    window = _window(qtbot, session)
    try:
        qtbot.waitUntil(
            lambda: qapp.palette().color(QPalette.ColorRole.Window).name() == OWN["background"],
            timeout=5000,
        )
    finally:
        _close(window)


# --- Keep configuration ---


def test_choosing_colors(
    qtbot: QtBot, qapp: QApplication, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = KeepConfigWindow(session)
    qtbot.addWidget(config)
    tabs = [config.tabs.tabText(i) for i in range(config.tabs.count())]
    assert tabs[-1] == "Appearance"
    assert config.color_mode.currentData() == "system"
    assert config.custom_colors.isHidden()

    # Its own: it starts from the colors shown now, and shows at once.
    shown = starting_colors()
    config.color_mode.setCurrentIndex(config.color_mode.findData("custom"))
    assert qapp.style().name().lower() == "fusion"  # before it's saved
    qtbot.waitUntil(lambda: _toml(session).get("appearance", {}) != {}, timeout=5000)
    appearance = _toml(session)["appearance"]
    assert appearance == {"colors": "custom", **shown}
    qtbot.waitUntil(lambda: not config.custom_colors.isHidden(), timeout=5000)

    # A color of its own, then one that reads poorly.
    monkeypatch.setattr(config, "choose_color", lambda label, current: "#c8963e")
    config.color_buttons["accent"].click()
    qtbot.waitUntil(lambda: config.color_buttons["accent"].text() == "#c8963e", timeout=5000)
    assert qapp.palette().color(QPalette.ColorRole.Highlight).name() == "#c8963e"
    assert config.color_buttons["accent"].text() == "#c8963e"
    background = session.keep.config.custom_colors["background"]
    monkeypatch.setattr(config, "choose_color", lambda label, current: background)
    config.color_buttons["text"].click()
    qtbot.waitUntil(lambda: not config.colors_warning.isHidden(), timeout=5000)
    assert "hard to read" in config.colors_warning.text()

    # Dark: its own colors are kept for later.
    config.color_mode.setCurrentIndex(config.color_mode.findData("dark"))
    qtbot.waitUntil(lambda: config.custom_colors.isHidden(), timeout=5000)
    assert session.keep.config.colors == "dark"
    assert session.keep.config.custom_colors["accent"] == "#c8963e"
    assert config.custom_colors.isHidden()
