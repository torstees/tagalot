"""Tests for creating and opening keeps."""

import logging
from pathlib import Path

import pytest

from tagalot.core import keep as keep_module
from tagalot.core.keep import (
    NETWORK_WARNING,
    KeepConfigError,
    KeepError,
    RootConfig,
    ThemeRef,
    create_keep,
    load_keep_config,
    open_keep,
)

THEME = ThemeRef(id="generic", version=1)


def _root(path: Path, root_id: str = "r1") -> RootConfig:
    return RootConfig(id=root_id, name="Files", path=str(path))


def test_create_then_open_round_trip(tmp_path: Path) -> None:
    root = _root(tmp_path / "files", "files")
    created = create_keep(tmp_path / "Music.keep", "Music", THEME, [root])

    assert created.toml_path.is_file()
    assert [p.name for p in created.dir.iterdir()] == ["keep.toml"]
    assert load_keep_config(created.toml_path) == created.config

    opened = open_keep(tmp_path / "Music.keep")
    assert opened == created
    assert opened.config.name == "Music"
    assert opened.config.theme == THEME
    assert opened.config.roots == [root]
    assert opened.on_network is False


def test_each_keep_gets_a_new_id(tmp_path: Path) -> None:
    a = create_keep(tmp_path / "a", "A", THEME)
    b = create_keep(tmp_path / "b", "B", THEME)
    assert a.config.id != b.config.id


def test_keep_file_locations(tmp_path: Path) -> None:
    keep = create_keep(tmp_path / "k", "K", THEME)
    assert keep.db_path == keep.dir / "keep.db"
    assert keep.thumbs_path == keep.dir / "thumbs.db"
    assert keep.ui_state_path == keep.dir / "ui_state.json"


def test_create_in_existing_empty_folder(tmp_path: Path) -> None:
    (tmp_path / "k").mkdir()
    assert create_keep(tmp_path / "k", "K", THEME).toml_path.is_file()


def test_create_makes_missing_parents(tmp_path: Path) -> None:
    assert create_keep(tmp_path / "a" / "b" / "k", "K", THEME).toml_path.is_file()


def test_create_refuses_non_empty_folder(tmp_path: Path) -> None:
    (tmp_path / "k").mkdir()
    (tmp_path / "k" / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(KeepError, match="not empty"):
        create_keep(tmp_path / "k", "K", THEME)
    assert (tmp_path / "k" / "notes.txt").read_text(encoding="utf-8") == "mine"


def test_create_refuses_a_file(tmp_path: Path) -> None:
    (tmp_path / "k").write_text("", encoding="utf-8")
    with pytest.raises(KeepError, match="not a folder"):
        create_keep(tmp_path / "k", "K", THEME)


def test_create_refuses_blank_name(tmp_path: Path) -> None:
    with pytest.raises(KeepError, match="needs a name"):
        create_keep(tmp_path / "k", "  ", THEME)
    assert not (tmp_path / "k").exists()


def test_create_validates_config_before_writing(tmp_path: Path) -> None:
    roots = [_root(tmp_path / "a", "dup"), _root(tmp_path / "b", "dup")]
    with pytest.raises(KeepConfigError, match="duplicate root id"):
        create_keep(tmp_path / "k", "K", THEME, roots)
    assert not (tmp_path / "k").exists()


@pytest.mark.parametrize(
    ("keep_rel", "root_rel"),
    [
        ("music/Music.keep", "music"),  # keep inside a root
        ("keeps", "keeps/inbox"),  # root inside the keep
        ("same", "same"),
    ],
)
def test_create_refuses_keep_and_root_inside_each_other(
    tmp_path: Path, keep_rel: str, root_rel: str
) -> None:
    # Roots need not exist yet (they may be unreachable shares).
    with pytest.raises(KeepError, match="inside one another"):
        create_keep(tmp_path / keep_rel, "K", THEME, [_root(tmp_path / root_rel)])


def test_sibling_folders_are_not_nested(tmp_path: Path) -> None:
    # "music" is a string prefix of "music.keep" but not a parent of it.
    create_keep(tmp_path / "music.keep", "K", THEME, [_root(tmp_path / "music")])


def test_open_missing_folder(tmp_path: Path) -> None:
    with pytest.raises(KeepError, match="does not exist"):
        open_keep(tmp_path / "nope")


def test_open_folder_without_keep_toml(tmp_path: Path) -> None:
    with pytest.raises(KeepError, match="is not a keep"):
        open_keep(tmp_path)


def test_open_invalid_keep_toml_is_a_keep_error(tmp_path: Path) -> None:
    (tmp_path / "keep.toml").write_text("[keep\n", encoding="utf-8")
    with pytest.raises(KeepError, match="invalid TOML"):
        open_keep(tmp_path)


def test_network_keep_opens_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    create_keep(tmp_path / "k", "K", THEME)
    monkeypatch.setattr(keep_module, "is_network_path", lambda path: True)
    with caplog.at_level(logging.WARNING):
        keep = open_keep(tmp_path / "k")
    assert keep.on_network is True
    assert NETWORK_WARNING in caplog.text
