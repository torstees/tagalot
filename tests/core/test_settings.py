"""Tests for per-user settings.toml."""

import logging
import sys
import tomllib
import uuid
from pathlib import Path

import pytest

from tagalot.core.keep import RootConfig
from tagalot.core.settings import (
    MAX_RECENT_KEEPS,
    HandlerOverride,
    Settings,
    command_placeholder_error,
    default_settings_path,
    dump_settings,
    load_settings,
    save_settings,
)

KEEP_A = uuid.UUID("0b6e3c1e-6f0a-4b54-9a8e-2b2d7f1c9d41")
KEEP_B = uuid.UUID("9d2a6f1b-1111-4c3d-8e8e-000000000002")
NAS = RootConfig(id="nas-music", name="NAS", path=r"\\nas\music")


def _full_settings() -> Settings:
    settings = Settings(
        recent_keeps=[Path(r"C:\Keeps\Music.keep"), Path("/home/ana/Art.keep")],
        theme_dirs=[Path(r"D:\tagalot-themes")],
        handlers=[
            HandlerOverride(ext=".psd", command=r'"C:\Tools\viewer.exe" "{path}"'),
            HandlerOverride(ext="FLAC", role="audio", command="foobar2000 /add {path}"),
        ],
    )
    settings.set_root_override(KEEP_A, "nas-music", r"M:\Music")
    settings.set_root_override(KEEP_A, "odd id with spaces", r"\\other\share's")
    settings.set_root_override(KEEP_B, "r1", "/Volumes/music")
    return settings


def test_default_path_is_in_user_config_dir() -> None:
    path = default_settings_path()
    assert path.name == "settings.toml"
    assert path.parent.name == "tagalot"
    if sys.platform == "win32":
        assert "Roaming" not in path.parts  # per-machine, not roaming


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "settings.toml") == Settings()


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config" / "tagalot" / "settings.toml"  # parent dirs are created
    save_settings(_full_settings(), path)
    assert load_settings(path) == _full_settings()


def test_output_is_hand_editable() -> None:
    text = dump_settings(_full_settings())
    assert f"[root_overrides.{KEEP_A}]" in text
    assert r"nas-music = 'M:\Music'" in text
    assert "'odd id with spaces' = " in text
    assert "ext = '.flac'" in text  # normalized
    assert tomllib.loads(text)["handlers"][1]["role"] == "audio"


def test_empty_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    save_settings(Settings(), path)
    assert load_settings(path) == Settings()


# --- recent keeps ---


def test_add_recent_moves_to_front_without_duplicates() -> None:
    s = Settings()
    for name in ["a", "b", "c", "a"]:
        s.add_recent_keep(Path(name).absolute())
    assert [p.name for p in s.recent_keeps] == ["a", "c", "b"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths are case-insensitive")
def test_add_recent_ignores_case_on_windows() -> None:
    s = Settings()
    s.add_recent_keep(Path(r"C:\Keeps\Music.keep"))
    s.add_recent_keep(Path(r"c:\keeps\MUSIC.KEEP"))
    assert s.recent_keeps == [Path(r"c:\keeps\MUSIC.KEEP")]


def test_recent_list_is_capped() -> None:
    s = Settings()
    for i in range(MAX_RECENT_KEEPS + 5):
        s.add_recent_keep(Path(f"k{i}").absolute())
    assert len(s.recent_keeps) == MAX_RECENT_KEEPS
    assert s.recent_keeps[0].name == f"k{MAX_RECENT_KEEPS + 4}"


def test_remove_recent_keep() -> None:
    s = Settings()
    s.add_recent_keep(Path("a").absolute())
    s.add_recent_keep(Path("b").absolute())
    s.remove_recent_keep(Path("a").absolute())
    s.remove_recent_keep(Path("missing").absolute())
    assert [p.name for p in s.recent_keeps] == ["b"]


# --- root overrides ---


@pytest.mark.parametrize(
    ("keep_id", "root", "expected"),
    [
        (KEEP_A, NAS, r"M:\Music"),  # override for this keep and root
        (KEEP_B, NAS, r"\\nas\music"),  # same root id, different keep: no override
        (KEEP_A, RootConfig(id="other", name="O", path="/x"), "/x"),  # different root
    ],
)
def test_root_path_resolution(keep_id: uuid.UUID, root: RootConfig, expected: str) -> None:
    assert _full_settings().root_path(keep_id, root) == expected


def test_clearing_last_override_removes_keep_entry() -> None:
    s = Settings()
    s.set_root_override(KEEP_A, "nas-music", r"M:\Music")
    s.set_root_override(KEEP_A, "nas-music", None)
    s.set_root_override(KEEP_B, "never-set", None)
    assert s.root_overrides == {}
    assert s.root_path(KEEP_A, NAS) == r"\\nas\music"


# --- file handler overrides ---


@pytest.mark.parametrize(
    ("ext", "role", "expected_command"),
    [
        (".psd", None, "psd-any"),
        ("PSD", "cover", "psd-any"),  # case-insensitive, no role-specific rule
        (".flac", "audio", "flac-audio"),  # role-specific wins
        (".flac", "cover", "flac-any"),
        (".flac", None, "flac-any"),
        (".mp3", "audio", None),
    ],
)
def test_handler_lookup(ext: str, role: str | None, expected_command: str | None) -> None:
    s = Settings(
        handlers=[
            HandlerOverride(ext=".psd", command="psd-any"),
            HandlerOverride(ext=".flac", command="flac-any"),
            HandlerOverride(ext=".flac", role="audio", command="flac-audio"),
        ]
    )
    handler = s.handler_for(ext, role)
    assert (handler.command if handler else None) == expected_command


@pytest.mark.parametrize(
    ("command", "problem"),
    [
        ('"viewer.exe" "{path}"', None),
        ("open {dir} --select {name}", None),
        ("echo {{literal}} {path}", None),
        ("viewer {file}", "unknown placeholder(s) file"),
        ("viewer {path", "unbalanced braces"),
        ("   ", "command is empty"),
    ],
)
def test_command_templates(command: str, problem: str | None) -> None:
    error = command_placeholder_error(command)
    assert (error is None) if problem is None else (problem in (error or ""))


# --- tolerance of bad files ---


def test_invalid_toml_is_set_aside(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "settings.toml"
    path.write_text("recent_keeps = [\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_settings(path) == Settings()
    assert not path.exists()
    assert (tmp_path / "settings.toml.invalid").read_text(encoding="utf-8") == "recent_keeps = [\n"
    assert "using defaults" in caplog.text


def test_bad_entries_are_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(
        f"""
recent_keeps = 'C:\\Keeps\\Music.keep'
theme_dirs = ['D:\\themes']
color = 'blue'

[root_overrides.not-a-uuid]
r1 = '/x'

[root_overrides.{KEEP_A}]
good = 'M:\\Music'
bad = 5

[[handlers]]
ext = '.psd'
command = 'viewer {{file}}'

[[handlers]]
command = 'viewer {{path}}'

[[handlers]]
ext = 'TXT'
command = 'notepad {{path}}'
""",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        s = load_settings(path)
    assert s.recent_keeps == []
    assert s.theme_dirs == [Path(r"D:\themes")]
    assert s.root_overrides == {KEEP_A: {"good": r"M:\Music"}}
    assert s.handlers == [HandlerOverride(ext=".txt", command="notepad {path}")]
    for expected in [
        "recent_keeps must be a list",
        "unknown key 'color'",
        "'not-a-uuid' is not a keep id",
        "root 'bad'",
        "#1 unknown placeholder(s) file",
        "#2 needs an ext",
    ]:
        assert expected in caplog.text
