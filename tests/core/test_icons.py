"""Icons for types and keeps (#556, DESIGN.md §12 *Icons*): which picture a theme's icon
name means, and what ``--check-theme`` says about one it can't find."""

from pathlib import Path

import pytest

from tagalot.builtin_themes.books import BooksTheme
from tagalot.builtin_themes.movies import Movie, MoviesTheme
from tagalot.builtin_themes.music import MusicTheme
from tagalot.core.icons import (
    GENERIC,
    icon_warnings,
    keep_icon,
    lucide_names,
    lucide_svg,
    resolve_icon,
    type_icon,
)
from tagalot.theme_tools import check_theme
from tagalot.themes.api import Entity, Theme
from tagalot.themes.loader import load_theme_file, load_themes, validate_theme

THEME = """
from tagalot.themes.api import Entity, Theme, role


class Wand(Entity):
    icon = "icons/wand.svg"
    roles = [role("file", kinds={"any"}, primary=True)]


class Spell(Entity):
    icon = "flim"
    roles = [role("file", kinds={"any"}, primary=True)]


class Scroll(Entity):
    icon = "icons/missing.svg"
    roles = [role("file", kinds={"any"}, primary=True)]


class MagicTheme(Theme):
    id, name = "magic", "Magic"
    icon = "wand-sparkles"
    entities = [Wand, Spell, Scroll]
"""

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M3 3h18"/></svg>'


@pytest.fixture
def magic(tmp_path: Path) -> Path:
    folder = tmp_path / "themes"
    (folder / "icons").mkdir(parents=True)
    (folder / "icons" / "wand.svg").write_text(SVG, encoding="utf-8")
    path = folder / "magic.py"
    path.write_text(THEME, encoding="utf-8")
    return path


def test_lucide_icons_are_bundled_with_their_license() -> None:
    assert {"film", "music", "disc", "user", "file", "image", "file-text"} <= lucide_names()
    svg = lucide_svg("film")
    assert svg is not None
    assert b"currentColor" in svg  # drawn in the palette's color
    assert lucide_svg("no-such-icon") is None
    assert GENERIC in lucide_names()


def test_built_in_themes_name_bundled_icons() -> None:
    catalog = load_themes(user_dir=Path("nonexistent"))
    for loaded in catalog.themes.values():
        theme = loaded.theme
        assert icon_warnings(theme) == [], theme.id
        assert keep_icon(theme) is not None, theme.id
        for entity in theme.entities:
            assert type_icon(theme, entity) is not None, (theme.id, entity.__name__)
    assert type_icon(MoviesTheme, Movie) == "film"
    assert keep_icon(MusicTheme) == "headphones"
    assert keep_icon(BooksTheme) == "library"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("film", "film"),
        (" film ", "film"),
        ("flim", None),
        ("", None),
        (None, None),
        (5, None),
        ("icons/wand.svg", "wand"),
        ("icons\\wand.svg", "wand"),
        ("icons/missing.svg", None),
        ("../themes/icons/wand.svg", None),  # only inside the theme's folder
    ],
)
def test_what_an_icon_name_means(magic: Path, value: object, expected: str | None) -> None:
    resolved = resolve_icon(value, magic.parent)
    if expected == "wand":
        assert resolved == str(magic.parent / "icons" / "wand.svg")
    else:
        assert resolved == expected


def test_a_themes_own_svg_is_beside_its_module(magic: Path) -> None:
    catalog = load_theme_file(magic)
    theme = catalog.themes["magic"].theme
    wand = next(e for e in theme.entities if e.__name__ == "Wand")
    assert type_icon(theme, wand) == str(magic.parent / "icons" / "wand.svg")
    assert keep_icon(theme) == "wand-sparkles"


def test_check_theme_warns_about_icons_it_cant_find(magic: Path) -> None:
    said: list[str] = []
    assert check_theme(magic, said.append) == 0  # warnings: it still loads
    warnings = [line for line in said if line.startswith("warning:")]
    assert warnings == [
        "warning: Spell.icon: 'flim' isn't one of the Lucide icons Tagalot includes "
        "(THEMES.md lists them); the generic icon shows instead",
        "warning: Scroll.icon: 'icons/missing.svg' isn't an SVG file in the theme's folder; "
        "the generic icon shows instead",
    ]


def test_an_icon_must_be_a_name() -> None:
    class Odd(Entity):
        icon = 5  # type: ignore[assignment]

    class OddTheme(Theme):
        id, name = "odd", "Odd"
        icon = ["film"]  # type: ignore[assignment]
        entities = [Odd]

    problems = validate_theme(OddTheme)
    assert "Odd: icon must be a Lucide icon's name or an SVG's path" in problems
    assert "the theme's icon must be a Lucide icon's name or an SVG's path" in problems
