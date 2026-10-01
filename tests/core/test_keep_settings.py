"""Keep-wide settings: name, thumbnail size, and theme options (#109)."""

import uuid
from pathlib import Path

import pytest

from tagalot.builtin_themes.assets2d import Assets2DTheme
from tagalot.core.keep import (
    KeepConfig,
    KeepError,
    RootConfig,
    ThemeRef,
    create_keep,
    load_keep_config,
    save_keep_config,
)
from tagalot.core.keep_settings import with_name, with_option, with_thumbnail_max
from tagalot.themes.api import Entity, Theme, option


class Thing(Entity):
    pass


class Options(Theme):
    id, name = "opts", "Options"
    entities = [Thing]
    options = [
        option("flag", False, label="Flag"),
        option("level", 1, label="Level"),
        option("ratio", 0.5, label="Ratio"),
        option("word", "a", label="Word"),
    ]


def _config(tmp_path: Path) -> KeepConfig:
    root = RootConfig("r", "R", str(tmp_path / "files"))
    return create_keep(tmp_path / "k", "K", ThemeRef("opts", 1), [root]).config


def test_renaming_the_keep(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert with_name(config, tmp_path / "k", "  New  ").name == "New"
    with pytest.raises(KeepError, match="needs a name"):
        with_name(config, tmp_path / "k", " ")


def test_thumbnail_size(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert with_thumbnail_max(config, 512).thumbnail_max == 512
    assert with_thumbnail_max(config, None).thumbnail_max is None
    for bad in (8, 4096):
        with pytest.raises(KeepError, match="16 to 2048"):
            with_thumbnail_max(config, bad)


@pytest.mark.parametrize(
    ("name", "value", "stored"),
    [("flag", True, True), ("level", 3, 3), ("ratio", 2, 2.0), ("word", "b", "b")],
)
def test_options_are_checked_and_stored(
    tmp_path: Path, name: str, value: object, stored: object
) -> None:
    config = _config(tmp_path)
    keep_wide = with_option(config, Options, name, value)
    assert keep_wide.theme_options == {name: stored}
    one_root = with_option(config, Options, name, value, root_id="r")
    assert one_root.roots[0].options == {name: stored}
    assert config.theme_options == {}  # copies: the original is untouched
    assert config.roots[0].options == {}


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("flag", 1, "Flag must be true or false"),
        ("level", 1.5, "Level must be a whole number"),
        ("level", True, "Level must be a whole number"),
        ("ratio", "x", "Ratio must be a number"),
        ("word", 3, "Word must be text"),
        ("nope", 1, "has no option 'nope'"),
    ],
)
def test_bad_option_values(tmp_path: Path, name: str, value: object, message: str) -> None:
    with pytest.raises(KeepError, match=message):
        with_option(_config(tmp_path), Options, name, value)


def test_removing_option_settings_and_saving(tmp_path: Path) -> None:
    config = with_option(_config(tmp_path), Options, "level", 2)
    config = with_option(config, Options, "level", 5, root_id="r")
    path = tmp_path / "k" / "keep.toml"
    save_keep_config(config, path)
    loaded = load_keep_config(path)
    assert (loaded.theme_options, loaded.roots[0].options) == ({"level": 2}, {"level": 5})
    cleared = with_option(
        with_option(loaded, Options, "level", None), Options, "level", None, root_id="r"
    )
    assert (cleared.theme_options, cleared.roots[0].options) == ({}, {})
    with pytest.raises(KeepError, match="no root"):
        with_option(config, Options, "level", 1, root_id="nope")


def test_a_real_theme_option() -> None:
    config = KeepConfig(id=uuid.uuid4(), name="A", theme=ThemeRef("assets2d", 1))
    assert with_option(config, Assets2DTheme, "artist_level", 2).theme_options == {
        "artist_level": 2
    }
