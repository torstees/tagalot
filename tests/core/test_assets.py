"""Assets folders (#418, DESIGN.md §9 *Markdown*): where a Markdown file's relative pictures
and links are looked for when they aren't beside it, and the settings naming them."""

import uuid
from pathlib import Path

import pytest

from tagalot.core.assets import asset_ref, assets_folders, clean_setting, in_assets
from tagalot.core.keep import (
    KeepConfig,
    KeepConfigError,
    RootConfig,
    ThemeRef,
    load_keep_config,
    save_keep_config,
)
from tagalot.core.note_markdown import find_links, read_pictures, renderable
from tagalot.core.theme_options import ASSETS_KEY, effective_options
from tagalot.themes.api import Theme


@pytest.mark.parametrize(
    ("relpath", "own", "defaults", "expected"),
    [
        ("Books/a.md", None, (), []),
        ("Books/a.md", "Attachments", (), ["Books/Attachments"]),
        ("Books/a.md", "../Shared/Assets", (), ["Shared/Assets"]),
        ("Books/a.md", "/Site/static/assets", (), ["Site/static/assets"]),
        ("Books/a.md", "Art\\Covers", (), ["Books/Art/Covers"]),  # Windows separators
        ("Books/a.md", None, ("Attachments", "Media"), ["Attachments", "Media"]),
        ("Books/a.md", None, ("./_assets", ""), ["Books/_assets"]),  # from the file's folder
        ("a.md", None, ("/Attachments",), ["Attachments"]),
        # its own first, then the folder's, then the keep's; repeats once
        ("Books/a.md", "Art", ("Media", "Art", "Media"), ["Books/Art", "Media", "Art"]),
        ("Books/a.md", "../../Outside", ("../Out",), []),  # never outside the folder
        ("Books/a.md", ".", (), []),  # beside the file: looked in first anyway
        ("Books/Deep/a.md", None, ("/",), [""]),  # the top of the watched folder
    ],
)
def test_assets_folders(
    relpath: str, own: str | None, defaults: tuple[str, ...], expected: list[str]
) -> None:
    assert assets_folders(relpath, own, defaults) == expected


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("covers/2024/dune.jpg", "covers/2024/dune.jpg"),
        ("covers\\dune.jpg", "covers/dune.jpg"),
        ("./dune.jpg", "dune.jpg"),
        ("/covers/dune.jpg", None),  # from the top of the folder: not an asset name
        ("../dune.jpg", None),
        ("https://example.com/dune.jpg", None),
        ("C:/dune.jpg", None),
        ("", None),
    ],
)
def test_asset_ref(ref: str, expected: str | None) -> None:
    assert asset_ref(ref) == expected


def test_in_assets() -> None:
    assert in_assets(["Art", ""], "covers/a.jpg") == ["Art/covers/a.jpg", "covers/a.jpg"]
    assert in_assets(["Art"], "../a.jpg") == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  Attachments ", "Attachments"),
        ("Site\\static\\assets", "Site/static/assets"),
        ("./_assets", "./_assets"),
        ("/Attachments", "/Attachments"),
        ("", ""),
    ],
)
def test_clean_setting(value: str, expected: str) -> None:
    assert clean_setting(value) == expected


@pytest.mark.parametrize("value", ["C:\\Pictures", "../Elsewhere", "//nas/share", "https://x"])
def test_a_setting_outside_the_folder_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="assets folder"):
        clean_setting(value)


# --- keep.toml ---


def _config(**fields: object) -> KeepConfig:
    return KeepConfig(
        id=uuid.UUID("0b6e3c1e-6f0a-4b54-9a8e-2b2d7f1c9d41"),
        name="Books",
        theme=ThemeRef(id="books", version=4),
        **fields,  # type: ignore[arg-type]
    )


def test_assets_settings_round_trip(tmp_path: Path) -> None:
    config = _config(
        roots=[
            RootConfig(id="a", name="A", path="/a", assets="Attachments"),
            RootConfig(id="b", name="B", path="/b"),
        ],
        markdown_assets="./_assets",
    )
    target = tmp_path / "keep.toml"
    save_keep_config(config, target)
    text = target.read_text(encoding="utf-8")
    assert "[markdown]\nassets = './_assets'" in text
    assert "assets = 'Attachments'" in text
    assert text.count("assets =") == 2  # a folder without one says nothing
    assert load_keep_config(target) == config


def test_no_assets_settings_write_nothing(tmp_path: Path) -> None:
    target = tmp_path / "keep.toml"
    save_keep_config(_config(roots=[RootConfig(id="a", name="A", path="/a")]), target)
    assert "assets" not in target.read_text(encoding="utf-8")


def test_an_assets_setting_must_be_text(tmp_path: Path) -> None:
    target = tmp_path / "keep.toml"
    save_keep_config(_config(), target)
    text = target.read_text(encoding="utf-8")
    target.write_text(text + "\n[markdown]\nassets = 3\n", encoding="utf-8")
    with pytest.raises(KeepConfigError, match=r"\[markdown\] assets must be a string"):
        load_keep_config(target)


# --- what ingest sees ---


class _Plain(Theme):
    id, name = "plain", "Plain"


def test_defaults_reach_ingest_only_when_set() -> None:
    """Carried with the theme options, so a changed default reads the folder again; a
    folder with none has the fingerprint it had before."""
    none = effective_options(_Plain, {}, {}, ("", ""))
    assert ASSETS_KEY not in none.values
    assert none.fingerprint() == effective_options(_Plain, {}, {}).fingerprint()
    both = effective_options(_Plain, {}, {}, ("Attachments", "Media"))
    assert both.values[ASSETS_KEY] == ["Attachments", "Media"]
    keep_only = effective_options(_Plain, {}, {}, ("", "Media"))
    assert keep_only.values[ASSETS_KEY] == ["Media"]
    assert both.fingerprint() != keep_only.fingerprint()


# --- a note's pictures and links ---


def _files(tmp_path: Path) -> tuple[Path, Path]:
    notes = tmp_path / "Notes"
    assets = tmp_path / "Attachments"
    (assets / "art").mkdir(parents=True)
    notes.mkdir()
    (assets / "art" / "sky.png").write_bytes(b"from assets")
    (assets / "both.png").write_bytes(b"from assets")
    (notes / "both.png").write_bytes(b"beside")
    (assets / "plan.pdf").write_bytes(b"pdf")
    return notes, assets


def test_pictures_from_the_assets_folder(tmp_path: Path) -> None:
    notes, assets = _files(tmp_path)
    body = renderable("![[art/sky.png]] ![b](both.png) ![gone](gone.png) ![up](../x.png)")
    assert read_pictures(body, str(notes)) == {"both.png": b"beside"}
    assert read_pictures(body, str(notes), [str(tmp_path / "Nowhere"), str(assets)]) == {
        "art/sky.png": b"from assets",
        "both.png": b"beside",  # beside the note comes first
    }


def test_links_found_in_the_assets_folder(tmp_path: Path) -> None:
    notes, assets = _files(tmp_path)
    body = renderable(
        "[plan](plan.pdf), [here](both.png), [gone](gone.pdf), [web](https://example.com), "
        "and ![a picture](art/sky.png)"
    )
    assert find_links(body, str(notes), []) == {}
    assert find_links(body, str(notes), [str(assets)]) == {
        str(notes / "plan.pdf"): str(assets / "plan.pdf"),  # only those that moved
    }
