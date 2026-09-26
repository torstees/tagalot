"""Tests for the shared TOML helpers."""

import tomllib
from pathlib import Path

import pytest

from tagalot.core.tomlio import toml_key, toml_list, toml_str, write_atomic


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (r"\\nas\music", r"'\\nas\music'"),
        ("plain", "'plain'"),
        ("It's", '"It\'s"'),
    ],
)
def test_toml_str(value: str, expected: str) -> None:
    assert toml_str(value) == expected
    assert tomllib.loads(f"v = {toml_str(value)}")["v"] == value


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("nas-music", "nas-music"),
        ("0b6e3c1e-6f0a-4b54-9a8e-2b2d7f1c9d41", "0b6e3c1e-6f0a-4b54-9a8e-2b2d7f1c9d41"),
        ("with space", "'with space'"),
        ("dotted.key", "'dotted.key'"),
        ("música", "'música'"),
    ],
)
def test_toml_key(key: str, expected: str) -> None:
    assert toml_key(key) == expected
    assert tomllib.loads(f"{toml_key(key)} = 1") == {key: 1}


def test_toml_list() -> None:
    assert tomllib.loads(f"v = {toml_list(['a', 'b\\c', ''])}")["v"] == ["a", "b\\c", ""]


def test_write_atomic_failure_keeps_old_file(tmp_path: Path) -> None:
    target = tmp_path / "f.toml"
    target.write_text("old", encoding="utf-8")
    # Writing a non-string fails after the temp file is created.
    with pytest.raises(TypeError):
        write_atomic(target, 123)  # type: ignore[arg-type]
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["f.toml"]
