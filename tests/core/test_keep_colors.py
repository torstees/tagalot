"""A keep's colors in keep.toml (#558, DESIGN.md §12 *Colors*): the system's, light, dark,
a color scheme's id (#572), or five written in keep.toml as #558 saved them."""

from pathlib import Path

import pytest

from tagalot.core.keep import (
    Keep,
    KeepConfigError,
    KeepError,
    ThemeRef,
    create_keep,
    dump_keep_config,
    load_keep_config,
    save_keep_config,
)
from tagalot.core.keep_settings import with_colors

OWN = {
    "background": "#1f1d1a",
    "text": "#ece6dc",
    "panels": "#2a2722",
    "accent": "#c8963e",
    "dimmed": "#9a9184",
}


@pytest.fixture
def keep(tmp_path: Path) -> Keep:
    return create_keep(tmp_path / "Books.keep", "Books", ThemeRef("books", 1))


def test_the_systems_colors_by_default(keep: Keep) -> None:
    assert (keep.config.colors, keep.config.custom_colors) == ("system", {})
    assert "colors" not in dump_keep_config(keep.config)


def test_round_trip(keep: Keep) -> None:
    save_keep_config(with_colors(keep.config, "custom", OWN), keep.toml_path)
    text = keep.toml_path.read_text(encoding="utf-8")
    assert "[appearance]\ncolors = 'custom'\nbackground = '#1f1d1a'\ntext = '#ece6dc'" in text
    loaded = load_keep_config(keep.toml_path)
    assert (loaded.colors, loaded.custom_colors) == ("custom", OWN)


def test_a_scheme_drops_the_colors_written_in_keep_toml(keep: Keep) -> None:
    config = with_colors(with_colors(keep.config, "custom", OWN), "nord")
    assert (config.colors, config.custom_colors) == ("nord", {})
    save_keep_config(config, keep.toml_path)
    assert "background" not in keep.toml_path.read_text(encoding="utf-8")
    assert load_keep_config(keep.toml_path).colors == "nord"


@pytest.mark.parametrize("scheme", ["nord", "books.library", "user:my-parchment"])
def test_schemes_are_named_in_keep_toml(keep: Keep, scheme: str) -> None:
    save_keep_config(with_colors(keep.config, scheme), keep.toml_path)
    assert load_keep_config(keep.toml_path).colors == scheme


def test_colors_558_kept_beside_another_mode_are_dropped(keep: Keep) -> None:
    text = keep.toml_path.read_text(encoding="utf-8")
    text += "\n[appearance]\ncolors = 'dark'\nbackground = '#1f1d1a'\n"
    keep.toml_path.write_text(text, encoding="utf-8")
    loaded = load_keep_config(keep.toml_path)
    assert (loaded.colors, loaded.custom_colors) == ("dark", {})


def test_colors_are_cleaned(keep: Keep) -> None:
    config = with_colors(keep.config, "custom", {"background": " #1F1D1A "})
    assert config.custom_colors == {"background": "#1f1d1a"}


@pytest.mark.parametrize(
    ("mode", "custom", "message"),
    [
        ("Sepia Tone", None, "no color scheme 'Sepia Tone'"),
        ("custom", {"background": "dark brown"}, "must look like #1f1d1a"),
        ("custom", {"background": "#123"}, "must look like #1f1d1a"),
        ("custom", {"border": "#123456"}, "No color named 'border'"),
    ],
)
def test_wrong_colors_are_refused(
    keep: Keep, mode: str, custom: dict[str, str] | None, message: str
) -> None:
    with pytest.raises(KeepError, match=message):
        with_colors(keep.config, mode, custom)


@pytest.mark.parametrize(
    "table",
    [
        "colors = 'Sepia Tone'",
        "colors = 5",
        "background = 'brown'",
        "accent = 5",
        "text = '#12345'",
    ],
)
def test_wrong_colors_in_keep_toml(keep: Keep, table: str) -> None:
    text = keep.toml_path.read_text(encoding="utf-8") + f"\n[appearance]\n{table}\n"
    keep.toml_path.write_text(text, encoding="utf-8")
    with pytest.raises(KeepConfigError):
        load_keep_config(keep.toml_path)
