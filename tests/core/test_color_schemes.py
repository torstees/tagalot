"""Color schemes (#572, DESIGN.md §12 *Colors*): built in, a theme's, and the user's own
files; what a keep's ``[appearance] colors`` names; theme checks."""

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import pytest

from tagalot.core.color_schemes import (
    BUILT_IN,
    ColorSchemeError,
    all_schemes,
    contrast,
    hard_to_read,
    name_problem,
    read_scheme,
    resolve_colors,
    save_user_scheme,
    scheme_colors,
    scheme_problems,
    scheme_warnings,
    slug_of,
    user_schemes,
)
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.keep_settings import with_colors
from tagalot.themes.api import ColorScheme, Entity, Theme
from tagalot.themes.loader import validate_theme

OWN = {
    "background": "#f3ece0",
    "text": "#2b2722",
    "panels": "#fffaf2",
    "accent": "#8a5a2b",
    "dimmed": "#7a6f61",
}


class Thing(Entity):
    pass


class LibraryTheme(Theme):
    id = "library"
    name = "Library"
    entities = (Thing,)
    color_schemes: ClassVar[Sequence[ColorScheme]] = (
        ColorScheme("reading-room", "Reading Room", *OWN.values()),
    )


def test_built_in_schemes_read_easily() -> None:
    assert len(BUILT_IN) >= 6
    assert len({s.id for s in BUILT_IN}) == len(BUILT_IN)
    for scheme in BUILT_IN:
        assert hard_to_read(scheme_colors(scheme)) == [], scheme.id
    assert scheme_problems(BUILT_IN) == []


def test_contrast() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21)
    assert contrast("#777777", "#777777") == pytest.approx(1)


def test_saving_and_reading_the_users_schemes(private_user_folders: Path) -> None:
    folder = private_user_folders / "color_schemes"  # tests/conftest.py
    assert user_schemes() == []
    scheme = save_user_scheme("My Parchment", OWN)
    assert scheme.id == "user:my-parchment"
    text = (folder / "my-parchment.toml").read_text(encoding="utf-8")
    assert text.startswith("name = 'My Parchment'\nbackground = '#f3ece0'\n")
    assert user_schemes() == [scheme]
    assert read_scheme(folder / "my-parchment.toml") == scheme  # as read from the file

    # Save: the same file, with new colors.
    changed = save_user_scheme("My Parchment", {**OWN, "accent": "#3e7cc8"}, scheme_id=scheme.id)
    assert changed.accent == "#3e7cc8"
    assert user_schemes() == [changed]
    assert len(list(folder.glob("*.toml"))) == 1


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "Give the scheme a name."),
        ("!!!", "a letter or digit"),
        ("my parchment", "You already have a scheme named 'my parchment'."),
    ],
)
def test_save_as_refuses_names(name: str, message: str) -> None:
    save_user_scheme("My Parchment", OWN)
    assert message in (name_problem(name) or "")
    with pytest.raises(ColorSchemeError, match=message):
        save_user_scheme(name, OWN)


def test_only_the_users_schemes_can_be_saved_over() -> None:
    with pytest.raises(ColorSchemeError, match="Save As"):
        save_user_scheme("Nord", OWN, scheme_id="nord")


def test_bad_colors_are_refused() -> None:
    with pytest.raises(ColorSchemeError, match="accent"):
        save_user_scheme("Mine", {**OWN, "accent": "blue"})


def test_files_that_cant_be_used_are_left_out(
    private_user_folders: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = private_user_folders / "color_schemes"
    folder.mkdir(parents=True)
    (folder / "broken.toml").write_text("name = ", encoding="utf-8")
    (folder / "pale.toml").write_text("name = 'Pale'\nbackground = 'white'\n", encoding="utf-8")
    good = "\n".join(f"{role} = '{value}'" for role, value in OWN.items())
    (folder / "good.toml").write_text(f"name = 'Good'\n{good}\n", encoding="utf-8")
    assert [s.id for s in user_schemes()] == ["user:good"]
    assert "Ignoring the color scheme" in caplog.text


def test_slugs() -> None:
    assert slug_of("My Parchment!") == "my-parchment"
    assert slug_of("  Café 2 ") == "caf-2"


def test_a_keep_offers_its_themes_schemes() -> None:
    save_user_scheme("Mine", OWN)
    ids = [s.id for s in all_schemes(LibraryTheme)]
    assert ids[: len(BUILT_IN)] == [s.id for s in BUILT_IN]
    assert ids[len(BUILT_IN) :] == ["library.reading-room", "user:mine"]
    assert "library.reading-room" not in [s.id for s in all_schemes(None)]


@pytest.mark.parametrize(
    ("colors", "expected"),
    [
        ("system", ("system", {})),
        ("dark", ("dark", {})),
        ("nord", ("custom", "#2e3440")),
        ("library.reading-room", ("custom", OWN["background"])),
        ("user:mine", ("custom", "#102030")),
        ("user:gone", ("system", {})),  # falls back
        ("other.scheme", ("system", {})),
    ],
)
def test_what_a_keep_names(tmp_path: Path, colors: str, expected: tuple[str, object]) -> None:
    save_user_scheme("Mine", {**OWN, "background": "#102030"})
    config = with_colors(
        create_keep(tmp_path / "A.keep", "A", ThemeRef("library", 1)).config, colors
    )
    mode, found = resolve_colors(config, LibraryTheme)
    assert mode == expected[0]
    assert (found.get("background") if found else {}) == expected[1]


def test_colors_written_in_keep_toml_still_show(tmp_path: Path) -> None:
    config = create_keep(tmp_path / "A.keep", "A", ThemeRef("library", 1)).config
    assert resolve_colors(with_colors(config, "custom", OWN), None) == ("custom", OWN)


def test_themes_schemes_are_checked() -> None:
    assert validate_theme(LibraryTheme) == []

    class Bad(LibraryTheme):
        color_schemes: ClassVar[Sequence[ColorScheme]] = (
            ColorScheme("Reading Room", "Reading", *OWN.values()),
            ColorScheme("dim", "Dim", "#000000", "#111111", "#000000", "#ffffff", "pink"),
            ColorScheme("dim", "Again", *OWN.values()),
        )

    problems = " ".join(validate_theme(Bad))
    assert "its id must be lowercase" in problems
    assert "dimmed must be a color" in problems
    assert "'dim' is declared twice" in problems

    class Faint(LibraryTheme):
        color_schemes: ClassVar[Sequence[ColorScheme]] = (
            ColorScheme("faint", "Faint", "#ffffff", "#dddddd", "#ffffff", "#3366cc", "#999999"),
        )

    assert validate_theme(Faint) == []  # it loads; --check-theme warns
    assert scheme_warnings(Faint)[0].startswith("color scheme 'faint': Text on the background")
