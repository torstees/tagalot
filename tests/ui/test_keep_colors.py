"""A keep's colors in the window (#558, DESIGN.md §12 *Colors*): a whole palette from five
colors, applied with Fusion while the keep's window is active, chosen in Keep
configuration's Appearance tab as a color scheme, changed, and saved as the user's own
(#572); named colors for errors, warnings, and stars."""

import importlib.util
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QProxyStyle
from pytestqt.qtbot import QtBot

from tagalot.core.color_schemes import BUILT_IN, hard_to_read, save_user_scheme, scheme_colors
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui import colors
from tagalot.ui.appearance import apply_colors, custom_palette, reset_colors
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
BUILT_IN_BY_ID = {s.id: s for s in BUILT_IN}


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


def _config(qtbot: QtBot, session: KeepSession) -> KeepConfigWindow:
    config = KeepConfigWindow(session)
    qtbot.addWidget(config)
    return config


def _choose(qtbot: QtBot, config: KeepConfigWindow, scheme: str) -> None:
    session = config.session
    config.color_mode.setCurrentIndex(config.color_mode.findData(scheme))
    qtbot.waitUntil(lambda: session.keep.config.colors == scheme, timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)


def _window_color(qapp: QApplication, role: QPalette.ColorRole = QPalette.ColorRole.Window) -> str:
    return qapp.palette().color(role).name()


def test_the_colors_list(qtbot: QtBot, session: KeepSession) -> None:
    config = _config(qtbot, session)
    tabs = [config.tabs.tabText(i) for i in range(config.tabs.count())]
    assert tabs[-2:] == ["Appearance", "Backups"]
    combo = config.color_mode
    listed = [combo.itemData(i) for i in range(combo.count()) if combo.itemText(i)]
    assert listed == ["system", "light", "dark", *(s.id for s in BUILT_IN)]
    assert combo.currentData() == "system"
    assert config.custom_colors.isHidden()


def test_choosing_a_scheme(qtbot: QtBot, qapp: QApplication, session: KeepSession) -> None:
    config = _config(qtbot, session)
    config.color_mode.setCurrentIndex(config.color_mode.findData("nord"))
    assert _window_color(qapp) == "#2e3440"  # before it's saved
    qtbot.waitUntil(lambda: _toml(session).get("appearance") == {"colors": "nord"}, timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)
    assert not config.custom_colors.isHidden()
    assert config.color_buttons["background"].text() == "#2e3440"
    assert config.save_scheme_button.isHidden()  # not the user's own
    assert config.save_scheme_as_button.isEnabled()
    _choose(qtbot, config, "dark")
    assert config.custom_colors.isHidden()


def test_editing_then_save_as_and_save(
    qtbot: QtBot,
    qapp: QApplication,
    session: KeepSession,
    monkeypatch: pytest.MonkeyPatch,
    private_user_folders: Path,
) -> None:
    config = _config(qtbot, session)
    _choose(qtbot, config, "nord")

    # A change shows at once, and isn't saved.
    monkeypatch.setattr(config, "choose_color", lambda label, current: "#c8963e")
    config.color_buttons["accent"].click()
    assert _window_color(qapp, QPalette.ColorRole.Highlight) == "#c8963e"
    assert config.color_buttons["accent"].text() == "#c8963e"
    assert "Save As" in config.scheme_state.text()
    assert session.keep.config.colors == "nord"

    # Save As: a scheme of the user's, which the keep then names.
    monkeypatch.setattr(config, "ask_scheme_name", lambda suggested: "Warm Nord")
    config.save_scheme_as_button.click()
    qtbot.waitUntil(lambda: session.keep.config.colors == "user:warm-nord", timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)
    saved = tomllib.loads(
        (private_user_folders / "color_schemes" / "warm-nord.toml").read_text(encoding="utf-8")
    )
    assert saved == {
        "name": "Warm Nord",
        **scheme_colors(BUILT_IN_BY_ID["nord"]),
        "accent": "#c8963e",
    }
    assert config.color_mode.currentText() == "Warm Nord"
    assert not config.save_scheme_button.isHidden()
    assert not config.save_scheme_button.isEnabled()  # nothing changed since
    assert config.scheme_state.text() == ""

    # Save: the user's own scheme, changed again.
    monkeypatch.setattr(config, "choose_color", lambda label, current: "#123456")
    config.color_buttons["panels"].click()
    assert config.save_scheme_button.isEnabled()
    config.save_scheme_button.click()
    qtbot.waitUntil(lambda: not config.save_scheme_button.isEnabled(), timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)
    expected = {k: v for k, v in saved.items() if k != "name"} | {"panels": "#123456"}
    assert session.colors == ("custom", expected)
    assert _window_color(qapp, QPalette.ColorRole.Base) == "#123456"


def test_save_as_refuses_a_name_taken(
    qtbot: QtBot, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_user_scheme("Mine", OWN)
    config = _config(qtbot, session)
    _choose(qtbot, config, "paper")
    monkeypatch.setattr(config, "ask_scheme_name", lambda suggested: "mine")
    config.save_scheme_as_button.click()
    assert "already have a scheme named 'mine'" in config.colors_warning.text()
    assert session.keep.config.colors == "paper"


def test_unsaved_changes_are_asked_about(
    qtbot: QtBot, qapp: QApplication, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(qtbot, session)
    _choose(qtbot, config, "nord")
    monkeypatch.setattr(config, "choose_color", lambda label, current: "#c8963e")
    config.color_buttons["accent"].click()

    # Keep them: the list goes back to the scheme being changed.
    monkeypatch.setattr(config, "discard_changes", lambda: False)
    config.color_mode.setCurrentIndex(config.color_mode.findData("paper"))
    assert config.color_mode.currentData() == "nord"
    assert config.color_buttons["accent"].text() == "#c8963e"
    assert session.keep.config.colors == "nord"

    # Discard them: the other scheme.
    monkeypatch.setattr(config, "discard_changes", lambda: True)
    _choose(qtbot, config, "paper")
    assert config.color_buttons["accent"].text() == "#2f6fbf"


def test_closing_drops_unsaved_changes(
    qtbot: QtBot, qapp: QApplication, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(qtbot, session)
    _choose(qtbot, config, "nord")
    monkeypatch.setattr(config, "choose_color", lambda label, current: "#c8963e")
    config.color_buttons["accent"].click()
    assert _window_color(qapp, QPalette.ColorRole.Highlight) == "#c8963e"
    config.close()  # the keep closing, say: not asked
    assert _window_color(qapp, QPalette.ColorRole.Highlight) == "#88c0d0"
    assert session.keep.config.colors == "nord"


def test_a_keeps_colors_from_558_until_saved_as_a_scheme(
    qtbot: QtBot, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.set_colors("custom", OWN)
    config = _config(qtbot, session)
    assert config.color_mode.currentText() == "This keep's colors"
    assert config.color_buttons["background"].text() == OWN["background"]
    assert "Save As keeps" in config.scheme_state.text()

    # Another scheme, and back: the keep's colors are offered until the window closes.
    _choose(qtbot, config, "slate")
    assert "background" not in _toml(session)["appearance"]  # type: ignore[operator]
    _choose(qtbot, config, "custom")
    assert session.keep.config.custom_colors == OWN

    monkeypatch.setattr(config, "ask_scheme_name", lambda suggested: "Old Colors")
    config.save_scheme_as_button.click()
    qtbot.waitUntil(lambda: session.keep.config.colors == "user:old-colors", timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)
    texts = [config.color_mode.itemText(i) for i in range(config.color_mode.count())]
    assert "This keep's colors" not in texts
    assert "Old Colors" in texts


def test_a_scheme_thats_gone(qtbot: QtBot, qapp: QApplication, session: KeepSession) -> None:
    session.set_colors("user:gone")
    assert session.colors == ("system", {})
    config = _config(qtbot, session)
    assert config.color_mode.currentText() == "user:gone (not found: the system's colors)"
    assert config.custom_colors.isHidden()


def test_the_keeps_window_keeps_showing_unsaved_changes(
    qtbot: QtBot, qapp: QApplication, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window(qtbot, session)
    try:
        config = window.configure_keep()
        _choose(qtbot, config, "nord")
        monkeypatch.setattr(config, "choose_color", lambda label, current: "#c8963e")
        config.color_buttons["accent"].click()
        window.apply_colors()  # as when the keep's window becomes active again
        assert _window_color(qapp, QPalette.ColorRole.Highlight) == "#c8963e"
        config.close()
        window.apply_colors()
        assert _window_color(qapp, QPalette.ColorRole.Highlight) == "#88c0d0"
    finally:
        _close(window)
