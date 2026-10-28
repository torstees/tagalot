"""Themes suggest fonts and a text size (#567, DESIGN.md §12 *Fonts*): fallback chains of
installed families, fonts downloaded into the keep, and files beside the theme; the keep's
own choice always wins."""

import importlib.util
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import ClassVar

import pytest
from PySide6.QtGui import QFont
from pytestqt.qtbot import QtBot

from tagalot.builtin_themes.books import BooksTheme
from tagalot.core.fonts import theme_font_files, theme_font_problems, theme_font_warnings
from tagalot.core.google_fonts import GoogleFont
from tagalot.core.keep import KeepConfig, ThemeRef, create_keep
from tagalot.core.keep_settings import with_fonts
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.themes.api import Entity, Theme
from tagalot.themes.loader import validate_theme
from tagalot.ui.fonts import PlaceFont, look_of, register_font, reset_fonts
from tagalot.ui.keep_config import KeepConfigWindow
from tests.core.test_keep_fonts import aileron

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


class Thing(Entity):
    pass


class Suggesting(Theme):
    id = "suggesting"
    name = "Suggesting"
    entities = (Thing,)
    fonts: ClassVar[Mapping[str, Sequence[str]]] = {
        "titles": ("No Such Font 12", "Literata", "serif"),
        "notes": ("fonts/Wand.ttf", "serif"),
        "text": ("No Such Font 12", "Also Not Here 7"),
    }
    text_size = 105


@pytest.fixture(autouse=True)
def system_fonts_after() -> Iterator[None]:
    yield
    reset_fonts()


@pytest.fixture
def family() -> str:
    found = register_font(aileron())
    assert found is not None
    return found


@pytest.fixture
def config(tmp_path: Path) -> KeepConfig:
    return create_keep(tmp_path / "A.keep", "A", ThemeRef("suggesting", 1)).config


def test_the_first_font_of_a_chain_thats_here(config: KeepConfig, family: str) -> None:
    files = {
        "fonts/Literata/Literata.ttf": aileron(),  # downloaded into the keep, named otherwise
        "theme:fonts/Wand.ttf": aileron(),
    }
    look, places = look_of(config, files, Suggesting)
    assert places["titles"] == PlaceFont(family, "theme", "Literata", ("No Such Font 12",))
    assert places["notes"] == PlaceFont(family, "theme", "fonts/Wand.ttf")
    assert places["text"] == PlaceFont(None, "theme", None, ("No Such Font 12", "Also Not Here 7"))
    assert look.size == 105


def test_a_generic_family_ends_a_chain(config: KeepConfig) -> None:
    _, places = look_of(config, {}, Suggesting)
    serif = QFont()
    serif.setStyleHint(QFont.StyleHint.Serif)
    assert places["titles"].family == serif.defaultFamily()
    assert places["titles"].missing == ("No Such Font 12", "Literata")


def test_the_keeps_choice_wins(config: KeepConfig, family: str) -> None:
    mine = with_fonts(config, {"titles": family, "notes": "system"}, 100)
    look, places = look_of(mine, {}, Suggesting)
    assert places["titles"] == PlaceFont(family, "keep", family)
    assert places["notes"] == PlaceFont(None, "keep", "system")
    assert look.size == 100  # the keep's, over the theme's 105


def test_no_theme_no_suggestions(config: KeepConfig) -> None:
    look, places = look_of(config, {}, None)
    assert {p.origin for p in places.values()} == {"system"}
    assert look.size == 100


def test_reading_a_themes_fonts(tmp_path: Path) -> None:
    keep = tmp_path / "A.keep"
    (keep / "fonts" / "Literata").mkdir(parents=True)
    (keep / "fonts" / "Literata" / "Literata.ttf").write_bytes(b"lit")
    (keep / "fonts" / "Literata" / "OFL.txt").write_bytes(b"license")
    theme_dir = tmp_path / "theme"
    (theme_dir / "fonts").mkdir(parents=True)
    (theme_dir / "fonts" / "Wand.ttf").write_bytes(b"wand")
    found = theme_font_files(
        keep, {**Suggesting.fonts, "text": ("../outside.ttf", "Gone 2")}, theme_dir
    )
    assert found == {"fonts/Literata/Literata.ttf": b"lit", "theme:fonts/Wand.ttf": b"wand"}


def test_themes_fonts_are_checked() -> None:
    assert validate_theme(Suggesting) == []

    class Wrong(Suggesting):
        fonts: ClassVar[Mapping[str, Sequence[str]]] = {"titles": "Literata"}
        text_size = 300

    problems = " ".join(validate_theme(Wrong))
    assert "fonts must map places" in problems
    assert "text_size must be a percent from 50 to 200" in problems
    assert theme_font_problems(BooksTheme) == []

    class Odd(Suggesting):
        fonts: ClassVar[Mapping[str, Sequence[str]]] = {"captions": ("serif",), "notes": ("x.otf",)}

    warnings = theme_font_warnings(Odd, None)
    assert "'captions' isn't a place for a font" in warnings[0]
    assert "'x.otf' isn't a file beside the theme" in warnings[1]


def test_the_built_in_books_theme_suggests() -> None:
    assert BooksTheme.fonts["titles"][0] == "Literata"
    assert BooksTheme.text_size == 105


# --- Keep configuration ---


@pytest.fixture
def books(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_books_demo(tmp_path), Settings()) as session:
        yield session


def test_keep_configuration_offers_the_themes_fonts(
    qtbot: QtBot, books: KeepSession, monkeypatch: pytest.MonkeyPatch, family: str
) -> None:
    config = KeepConfigWindow(books)
    qtbot.addWidget(config)
    assert config.text_size.value() == 105  # the theme's
    titles = config.font_buttons["titles"].text()
    assert titles.endswith("(the theme's)") or titles == "The system's"
    assert not config.theme_font_buttons["titles"].isHidden()
    assert config.theme_font_buttons["text"].isHidden()  # books suggests none for the text
    note = config.fonts_note.text()
    assert "Titles: the theme's Literata" in note  # not downloaded
    download = config.download_font_buttons["titles"]
    assert not download.isHidden()
    assert download.text() == "Download Literata"

    # Downloading it: then the theme's chain finds it in the keep.
    asked: list[str] = []

    def fake(font: GoogleFont, progress: object = None) -> str:
        asked.append(font.name)
        folder = books.keep.dir / "fonts" / font.keep_folder
        folder.mkdir(parents=True)
        (folder / "Literata.ttf").write_bytes(aileron())
        books.font_files = books._read_font_files(books.keep.config)
        return font.value

    monkeypatch.setattr(books, "download_google_font", fake)
    download.click()
    qtbot.waitUntil(lambda: config.download_font_buttons["titles"].isHidden(), timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)
    assert asked == ["Literata"]
    assert config.font_buttons["titles"].text() == f"{family} (the theme's)"
    assert books.keep.config.fonts == {}  # still the theme's suggestion, not a choice

    # The text size back at the theme's isn't written.
    config.text_size.setValue(110)
    qtbot.waitUntil(lambda: books.keep.config.text_size == 110, timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)
    config.text_size.setValue(105)
    qtbot.waitUntil(lambda: books.keep.config.text_size is None, timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)
