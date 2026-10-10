"""Tests for reading and writing keep.toml."""

import logging
import tomllib
import uuid
from pathlib import Path

import pytest

from tagalot.core.keep import (
    KEEP_FORMAT_VERSION,
    KeepConfig,
    KeepConfigError,
    RootConfig,
    ThemeRef,
    dump_keep_config,
    load_keep_config,
    save_keep_config,
)

# The example from DESIGN.md §4, verbatim.
DESIGN_EXAMPLE = """\
[keep]
id = "0b6e3c1e-6f0a-4b54-9a8e-2b2d7f1c9d41"   # UUID, never changes
name = "Music"
format_version = 15                         # core schema version

[theme]
id = "music"
version = 1                                 # theme schema version

[[roots]]
id = "nas-music"                            # stable, referenced by resources
name = "NAS music share"
path = '\\\\nas\\music'
exclude = ["**/.DS_Store", "**/Thumbs.db", "**/@eaDir/**"]
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "keep.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _config(*roots: RootConfig) -> KeepConfig:
    return KeepConfig(
        id=uuid.UUID("0b6e3c1e-6f0a-4b54-9a8e-2b2d7f1c9d41"),
        name="Music",
        theme=ThemeRef(id="music", version=1),
        roots=list(roots),
    )


def test_reads_design_example(tmp_path: Path) -> None:
    config = load_keep_config(_write(tmp_path, DESIGN_EXAMPLE))
    assert config == _config(
        RootConfig(
            id="nas-music",
            name="NAS music share",
            path=r"\\nas\music",
            exclude=["**/.DS_Store", "**/Thumbs.db", "**/@eaDir/**"],
        )
    )


@pytest.mark.parametrize(
    "path",
    [
        r"\\nas\music",  # UNC share
        r"M:\Music",  # mapped drive
        r"C:\Users\Ana\Music\It's Mine",  # a single quote forces a basic string
        "/Volumes/Music",  # macOS mount
        "/mnt/nas/música ♪",  # non-ASCII
        "tab\there",  # control character forces a basic string
    ],
)
def test_round_trip_preserves_paths(tmp_path: Path, path: str) -> None:
    config = _config(RootConfig(id="r1", name="Root", path=path, exclude=["**/x'y", r"a\b"]))
    target = tmp_path / "keep.toml"
    save_keep_config(config, target)
    assert load_keep_config(target) == config


def test_windows_paths_are_written_as_literal_strings() -> None:
    text = dump_keep_config(_config(RootConfig(id="nas", name="NAS", path=r"\\nas\music")))
    assert r"path = '\\nas\music'" in text
    assert tomllib.loads(text)["roots"][0]["path"] == r"\\nas\music"


def test_zero_roots_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "keep.toml"
    save_keep_config(_config(), target)
    assert load_keep_config(target).roots == []
    assert "[[roots]]" not in target.read_text(encoding="utf-8")


def test_new_config_uses_current_format_version() -> None:
    assert _config().format_version == KEEP_FORMAT_VERSION == 15


def test_exclude_defaults_to_empty(tmp_path: Path) -> None:
    exclude_line = 'exclude = ["**/.DS_Store", "**/Thumbs.db", "**/@eaDir/**"]\n'
    text = DESIGN_EXAMPLE.replace(exclude_line, "")
    assert load_keep_config(_write(tmp_path, text)).roots[0].exclude == []


def test_save_replaces_existing_file_and_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "keep.toml"
    save_keep_config(_config(), target)
    renamed = _config()
    renamed.name = "Renamed"
    save_keep_config(renamed, target)
    assert load_keep_config(target).name == "Renamed"
    assert [p.name for p in tmp_path.iterdir()] == ["keep.toml"]


def test_save_writes_lf_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "keep.toml"
    save_keep_config(_config(RootConfig(id="r", name="R", path="/x")), target)
    assert b"\r\n" not in target.read_bytes()


def test_unknown_keys_are_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = DESIGN_EXAMPLE + "\n[future]\nx = 1\n"
    text = text.replace('name = "Music"', 'name = "Music"\ncolor = "red"')
    with caplog.at_level(logging.WARNING):
        config = load_keep_config(_write(tmp_path, text))
    assert config.name == "Music"
    assert "'future'" in caplog.text
    assert "'color'" in caplog.text


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(KeepConfigError, match="cannot read file"):
        load_keep_config(tmp_path / "keep.toml")


def test_invalid_toml(tmp_path: Path) -> None:
    with pytest.raises(KeepConfigError, match="invalid TOML"):
        load_keep_config(_write(tmp_path, "[keep\n"))


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("[keep]", "[kept]", r"missing \[keep\] table"),
        ("[theme]", "[themes]", r"missing \[theme\] table"),
        ('id = "0b6e3c1e-6f0a-4b54-9a8e-2b2d7f1c9d41"', 'id = "not-a-uuid"', "not a UUID"),
        ('name = "Music"', 'name = ""', r"\[keep\] name must be a non-empty string"),
        ('name = "Music"', "name = 5", r"\[keep\] name must be a non-empty string"),
        ("format_version = 15", 'format_version = "3"', "format_version must be a positive"),
        ("format_version = 15", "format_version = 0", "format_version must be a positive"),
        ('id = "music"', "id = 3", r"\[theme\] id must be a non-empty string"),
        ("\nversion = 1 ", "\nversion = true ", r"\[theme\] version must be a positive"),
        ('id = "nas-music"', 'id = "  "', r"\[\[roots\]\] #1 id must be a non-empty"),
        ("path = '\\\\nas\\music'\n", "", r"\[\[roots\]\] #1 path must be a non-empty"),
        ('exclude = ["**/.DS_Store"', 'exclude = [1, "**/.DS_Store"', "exclude must be a list"),
    ],
)
def test_invalid_values(tmp_path: Path, old: str, new: str, message: str) -> None:
    assert old in DESIGN_EXAMPLE
    with pytest.raises(KeepConfigError, match=message):
        load_keep_config(_write(tmp_path, DESIGN_EXAMPLE.replace(old, new, 1)))


def test_roots_must_be_tables(tmp_path: Path) -> None:
    # A top-level key must come before the first table header.
    text = "roots = ['/music']\n" + DESIGN_EXAMPLE.split("[[roots]]")[0]
    with pytest.raises(KeepConfigError, match=r"roots must be written as \[\[roots\]\] tables"):
        load_keep_config(_write(tmp_path, text))


def test_duplicate_root_ids(tmp_path: Path) -> None:
    second = '\n[[roots]]\nid = "nas-music"\nname = "Other"\npath = "/other"\n'
    with pytest.raises(KeepConfigError, match="duplicate root id 'nas-music'"):
        load_keep_config(_write(tmp_path, DESIGN_EXAMPLE + second))


def test_error_includes_path(tmp_path: Path) -> None:
    path = _write(tmp_path, "[keep\n")
    with pytest.raises(KeepConfigError) as info:
        load_keep_config(path)
    assert info.value.path == path
    assert str(path) in str(info.value)


def test_thumbnail_max_round_trip(tmp_path: Path) -> None:
    config = _config()
    assert "[thumbnails]" not in dump_keep_config(config)  # absent unless set
    config.thumbnail_max = 512
    path = tmp_path / "keep.toml"
    save_keep_config(config, path)
    assert "[thumbnails]\nmax_size = 512" in path.read_text(encoding="utf-8")
    assert load_keep_config(path).thumbnail_max == 512


@pytest.mark.parametrize("value", ["0", '"big"', "true"])
def test_invalid_thumbnail_max(tmp_path: Path, value: str) -> None:
    text = DESIGN_EXAMPLE.replace("[[roots]]", f"[thumbnails]\nmax_size = {value}\n\n[[roots]]")
    with pytest.raises(KeepConfigError, match=r"\[thumbnails\] max_size must be a positive"):
        load_keep_config(_write(tmp_path, text))
