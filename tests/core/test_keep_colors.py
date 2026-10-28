"""A keep's colors in keep.toml (#558, DESIGN.md §12 *Colors*): the system's, light, dark,
or its own five."""

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


def test_its_own_colors_are_kept_while_another_mode_is_chosen(keep: Keep) -> None:
    config = with_colors(with_colors(keep.config, "custom", OWN), "dark")
    assert (config.colors, config.custom_colors) == ("dark", OWN)
    save_keep_config(config, keep.toml_path)
    assert load_keep_config(keep.toml_path).custom_colors == OWN
    assert with_colors(config, "custom").custom_colors == OWN  # back to them


def test_colors_are_cleaned(keep: Keep) -> None:
    config = with_colors(keep.config, "custom", {"background": " #1F1D1A "})
    assert config.custom_colors == {"background": "#1f1d1a"}


@pytest.mark.parametrize(
    ("mode", "custom", "message"),
    [
        ("sepia", None, "system, light, dark, or custom"),
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
    ["colors = 'sepia'", "background = 'brown'", "accent = 5", "text = '#12345'"],
)
def test_wrong_colors_in_keep_toml(keep: Keep, table: str) -> None:
    text = keep.toml_path.read_text(encoding="utf-8") + f"\n[appearance]\n{table}\n"
    keep.toml_path.write_text(text, encoding="utf-8")
    with pytest.raises(KeepConfigError):
        load_keep_config(keep.toml_path)
