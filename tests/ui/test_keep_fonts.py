"""A keep's fonts in the window (#565, DESIGN.md §12 *Fonts*): titles, notes, and text, and a
text size, applied while the keep's window is active and chosen in Keep configuration's
Appearance tab."""

import importlib.util
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QLabel
from pytestqt.qtbot import QtBot

from tagalot.core.google_fonts import GoogleFont, GoogleFontError, Progress
from tagalot.core.keep_settings import with_fonts
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.font_picker import FontChoice, FontPicker
from tagalot.ui.fonts import (
    FontLook,
    apply_fonts,
    follow,
    installed,
    look_of,
    place_font,
    register_font,
    reset_fonts,
)
from tagalot.ui.keep_config import KeepConfigWindow
from tagalot.ui.main_window import MainWindow
from tagalot.ui.title_card import _fit_title
from tagalot.ui.workers import ScanController
from tests.core.test_keep_fonts import aileron

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


@pytest.fixture(autouse=True)
def system_fonts_after(qapp: QApplication) -> Iterator[None]:
    """Every test leaves the application with the system's fonts."""
    reset_fonts()
    before = QFont(qapp.font())
    yield
    reset_fonts()
    assert qapp.font() == before


@pytest.fixture
def family() -> str:
    """A family that's there: Aileron, registered from Pillow's copy."""
    found = register_font(aileron())
    assert found is not None
    return found


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_music_demo(tmp_path), Settings()) as session:
        yield session


def _toml(session: KeepSession) -> dict[str, object]:
    return tomllib.loads(session.keep.toml_path.read_text(encoding="utf-8"))


def _fonts(session: KeepSession) -> object:
    appearance = _toml(session).get("appearance", {})
    return appearance.get("fonts") if isinstance(appearance, dict) else None


# --- the look ---


def test_a_keeps_fonts_and_whats_missing(session: KeepSession, family: str) -> None:
    assert installed(family)
    assert installed(family.upper())
    config = with_fonts(
        session.keep.config,
        {"titles": family, "notes": "No Such Font 12", "text": "fonts/gone.ttf"},
        120,
    )
    look, places = look_of(config, {"fonts/gone.ttf": None})
    assert look == FontLook(titles=family, size=120)
    missing = {p: f.missing for p, f in places.items() if f.missing}
    assert missing == {"notes": ("No Such Font 12",), "text": ("fonts/gone.ttf",)}
    assert {f.origin for f in places.values()} == {"keep"}
    file_look, _ = look_of(
        with_fonts(config, {"notes": "fonts/A.ttf"}, None), {"fonts/A.ttf": aileron()}
    )
    assert file_look.notes == family


def test_applying_fonts_and_back(qapp: QApplication, family: str) -> None:
    size = qapp.font().pointSizeF()
    assert apply_fonts(FontLook(text=family, size=150))
    assert qapp.font().family() == family
    assert qapp.font().pointSizeF() == pytest.approx(size * 1.5)
    assert not apply_fonts(FontLook(text=family, size=150))  # the same: nothing to do
    assert place_font("titles").family() == family  # the text's, without its own
    apply_fonts(FontLook(titles=family))
    assert place_font("titles", bold=True).family() == family
    assert place_font("titles", bold=True).bold()
    assert place_font("notes").family() != family


def test_widgets_follow_the_fonts(qtbot: QtBot, qapp: QApplication, family: str) -> None:
    label = QLabel("A title")
    qtbot.addWidget(label)
    follow(label, lambda: place_font("titles", larger=5))
    size = label.font().pointSizeF()
    apply_fonts(FontLook(titles=family, size=200))
    assert label.font().family() == family
    assert label.font().pointSizeF() == pytest.approx(qapp.font().pointSizeF() + 5)
    assert label.font().pointSizeF() > size


def test_title_cards_use_the_titles_font(family: str) -> None:
    apply_fonts(FontLook(titles=family))
    font, lines = _fit_title("The Colour of Magic", 256, 200, 200)
    assert font.family() == family
    assert lines


# --- the window ---


def test_the_window_shows_its_keeps_fonts(
    qtbot: QtBot, qapp: QApplication, session: KeepSession, family: str
) -> None:
    size = qapp.font().pointSizeF()
    session.set_fonts({"text": family, "titles": family}, 120)
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.show()
    try:
        qtbot.waitUntil(lambda: qapp.font().family() == family, timeout=5000)
        assert qapp.font().pointSizeF() == pytest.approx(size * 1.2)
        headings = window.navigation._headings.values()
        assert all(h.font().family() == family for h in headings)
    finally:
        window.thumbnails.clear()
        window.thumbnails.wait()


# --- Keep configuration ---


def test_choosing_fonts(
    qtbot: QtBot,
    qapp: QApplication,
    session: KeepSession,
    family: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = KeepConfigWindow(session)
    qtbot.addWidget(config)
    assert config.font_buttons["notes"].text() == "The system's"  # music suggests none
    assert not config.system_font_buttons["notes"].isEnabled()
    assert config.theme_font_buttons["notes"].isHidden()
    assert config.font_buttons["titles"].text().endswith("(the theme's)")
    assert not config.theme_font_buttons["titles"].isEnabled()  # already the theme's
    assert config.text_size.value() == 100

    # A file: copied into the keep folder.
    source = tmp_path / "Aileron-Regular.ttf"
    source.write_bytes(aileron())
    seen: list[list[str]] = []

    def choose(place: str, current: str | None, samples: list[str]) -> FontChoice:
        seen.append(samples)
        return FontChoice(file=source)

    monkeypatch.setattr(config, "choose_font", choose)
    config.font_buttons["titles"].click()
    qtbot.waitUntil(
        lambda: _fonts(session) == {"titles": "fonts/Aileron-Regular.ttf"}, timeout=5000
    )
    qtbot.waitUntil(lambda: config.font_buttons["titles"].text() == family, timeout=5000)
    assert seen[0][0] == session.keep.config.name  # the keep's own titles to preview
    assert (session.keep.dir / "fonts" / "Aileron-Regular.ttf").read_bytes() == aileron()
    assert config.system_font_buttons["titles"].isEnabled()

    # An installed family, and the text's size.
    monkeypatch.setattr(config, "choose_font", lambda p, c, s: FontChoice(family=family))
    config.font_buttons["notes"].click()
    qtbot.waitUntil(lambda: session.keep.config.fonts.get("notes") == family, timeout=5000)
    config.text_size.setValue(110)
    qtbot.waitUntil(lambda: session.keep.config.text_size == 110, timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)

    # The system's, over the theme's suggestion; then the theme's again.
    config.system_font_buttons["titles"].click()
    qtbot.waitUntil(lambda: session.keep.config.fonts.get("titles") == "system", timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)
    assert config.font_buttons["titles"].text() == "The system's"
    assert _fonts(session) == {"titles": "system", "notes": family, "text_size": 110}
    config.theme_font_buttons["titles"].click()
    qtbot.waitUntil(lambda: "titles" not in session.keep.config.fonts, timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)
    assert config.font_buttons["titles"].text().endswith("(the theme's)")


def test_a_font_thats_not_here(qtbot: QtBot, session: KeepSession) -> None:
    session.set_fonts({"titles": "No Such Font 12", "notes": "fonts/gone.ttf"}, None)
    config = KeepConfigWindow(session)
    qtbot.addWidget(config)
    assert config.font_buttons["titles"].text() == "No Such Font 12 (not here)"
    note = config.fonts_note.text()
    assert "Titles: No Such Font 12 isn't installed on this computer" in note
    assert "Notes: fonts/gone.ttf isn't in the keep folder" in note


def test_the_picker(qtbot: QtBot, family: str) -> None:
    picker = FontPicker("titles", family, ["Books", "The Colour of Magic"])
    qtbot.addWidget(picker)
    assert picker.family() == family
    assert picker.preview.font().family() == family
    assert "The Colour of Magic" in picker.preview.text()
    picker.filter.setText(family[:4].lower())
    assert picker.proxy.rowCount() >= 1
    picker.accept()
    assert picker.choice == FontChoice(family=family)


# --- Google Fonts (#566) ---


def test_finding_a_google_font(qtbot: QtBot) -> None:
    picker = FontPicker("titles", None, ["Books"], here={"Lato"})
    qtbot.addWidget(picker)
    picker.tabs.setCurrentIndex(1)
    picker.category.setCurrentIndex(picker.category.findData("serif"))
    picker.select_google("Literata")
    font = picker.google_font()
    assert font is not None
    assert font.name == "Literata"
    assert all(
        picker.google_proxy.index(r, 0).data(Qt.ItemDataRole.UserRole + 1).category == "serif"
        for r in range(picker.google_proxy.rowCount())
    )
    assert picker.download_button.text() == "Download"
    assert "raw.githubusercontent.com" in picker.google_status.text()
    if not installed("Literata"):
        assert picker.preview_note.text() == "Shown in a similar font until it's downloaded."
    picker.category.setCurrentIndex(0)
    picker.select_google("Lato")
    assert picker.download_button.text() == "Use"  # already in the keep
    assert "in this keep" in picker.google_list.currentIndex().data()


def test_downloading_a_google_font(qtbot: QtBot) -> None:
    asked: list[str] = []

    def download(font: GoogleFont, progress: Progress) -> str:
        asked.append(font.name)
        progress(1, 3)
        return font.value

    picker = FontPicker("titles", None, ["Books"], download=download)
    qtbot.addWidget(picker)
    picker.tabs.setCurrentIndex(1)
    picker.select_google("Literata")
    with qtbot.waitSignal(picker.finished, timeout=5000):
        picker.download_button.click()
    assert asked == ["Literata"]
    assert picker.choice == FontChoice(value="fonts/Literata/Literata.ttf")


def test_a_download_that_fails(qtbot: QtBot) -> None:
    def download(font: GoogleFont, progress: Progress) -> str:
        raise GoogleFontError("Couldn't reach raw.githubusercontent.com: offline.")

    picker = FontPicker("titles", None, ["Books"], download=download)
    qtbot.addWidget(picker)
    picker.tabs.setCurrentIndex(1)
    picker.select_google("Literata")
    picker.download_button.click()
    qtbot.waitUntil(lambda: picker.download_button.isEnabled(), timeout=5000)
    assert "offline. You can pick an installed font instead" in picker.google_status.text()
    assert picker.choice is None
    assert not picker.progress.isVisible()


def test_choosing_a_downloaded_font(
    qtbot: QtBot, session: KeepSession, family: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = session.keep.dir / "fonts" / "Aileron"
    folder.mkdir(parents=True)
    (folder / "Aileron-Regular.ttf").write_bytes(aileron())
    config = KeepConfigWindow(session)
    qtbot.addWidget(config)
    value = "fonts/Aileron/Aileron-Regular.ttf"
    monkeypatch.setattr(config, "choose_font", lambda p, c, s: FontChoice(value=value))
    config.font_buttons["titles"].click()
    qtbot.waitUntil(lambda: config.font_buttons["titles"].text() == family, timeout=5000)
    assert session.keep.config.fonts == {"titles": value}
    assert session.font_files[value] == aileron()
