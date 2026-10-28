"""A keep's fonts in keep.toml and its fonts folder (#565, DESIGN.md §12 *Fonts*)."""

import base64
import inspect
import re
from pathlib import Path

import pytest
from PIL import ImageFont

from tagalot.core.fonts import (
    FontFileError,
    copy_font_file,
    font_path,
    is_font_file,
    read_font_files,
)
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
from tagalot.core.keep_settings import with_fonts


def aileron() -> bytes:
    """Aileron Regular (SIL Open Font License), the font Pillow embeds as its default."""
    found = re.search(r'b"""(.*?)"""', inspect.getsource(ImageFont.load_default), re.DOTALL)
    assert found is not None
    return base64.b64decode(found.group(1))


@pytest.fixture
def keep(tmp_path: Path) -> Keep:
    return create_keep(tmp_path / "Books.keep", "Books", ThemeRef("books", 1))


def test_the_systems_fonts_by_default(keep: Keep) -> None:
    assert (keep.config.fonts, keep.config.text_size) == ({}, None)
    assert "fonts" not in dump_keep_config(keep.config)


def test_round_trip(keep: Keep) -> None:
    config = with_fonts(
        keep.config, {"notes": "fonts/Literata.ttf", "titles": " Literata ", "text": None}, 110
    )
    save_keep_config(config, keep.toml_path)
    text = keep.toml_path.read_text(encoding="utf-8")
    assert (
        "[appearance.fonts]\ntitles = 'Literata'\nnotes = 'fonts/Literata.ttf'\ntext_size = 110"
        in text
    )
    loaded = load_keep_config(keep.toml_path)
    assert loaded.fonts == {"titles": "Literata", "notes": "fonts/Literata.ttf"}
    assert loaded.text_size == 110


def test_sizes_and_empty_places(keep: Keep) -> None:
    # 100 is kept: it's the keep's own over a theme's suggestion (#567).
    assert with_fonts(keep.config, {}, 100).text_size == 100
    assert with_fonts(keep.config, {}, None).text_size is None
    assert with_fonts(keep.config, {"titles": ""}, None).fonts == {}


@pytest.mark.parametrize(
    ("fonts", "size", "message"),
    [
        ({"captions": "Arial"}, None, "no place for a font called 'captions'"),
        ({}, 40, "from 50% to 200%"),
        ({}, 250, "from 50% to 200%"),
    ],
)
def test_wrong_fonts_are_refused(
    keep: Keep, fonts: dict[str, str | None], size: int | None, message: str
) -> None:
    with pytest.raises(KeepError, match=message):
        with_fonts(keep.config, fonts, size)


@pytest.mark.parametrize(
    "table",
    ["titles = 5", "notes = ''", "text_size = 300", "text_size = true", "text_size = '110'"],
)
def test_wrong_fonts_in_keep_toml(keep: Keep, table: str) -> None:
    text = keep.toml_path.read_text(encoding="utf-8") + f"\n[appearance.fonts]\n{table}\n"
    keep.toml_path.write_text(text, encoding="utf-8")
    with pytest.raises(KeepConfigError):
        load_keep_config(keep.toml_path)


def test_font_files_are_copied_once(keep: Keep, tmp_path: Path) -> None:
    source = tmp_path / "Aileron-Regular.ttf"
    source.write_bytes(aileron())
    assert copy_font_file(keep.dir, source) == "fonts/Aileron-Regular.ttf"
    assert copy_font_file(keep.dir, source) == "fonts/Aileron-Regular.ttf"  # the same file
    other = tmp_path / "other" / "Aileron-Regular.ttf"
    other.parent.mkdir()
    other.write_bytes(aileron() + b"\0")
    assert copy_font_file(keep.dir, other) == "fonts/Aileron-Regular 2.ttf"
    assert (keep.dir / "fonts" / "Aileron-Regular.ttf").read_bytes() == aileron()


def test_only_font_files_are_copied(keep: Keep, tmp_path: Path) -> None:
    source = tmp_path / "font.woff2"
    source.write_bytes(b"x")
    with pytest.raises(FontFileError, match="TrueType"):
        copy_font_file(keep.dir, source)
    with pytest.raises(FontFileError, match="Couldn't read"):
        copy_font_file(keep.dir, tmp_path / "gone.ttf")


def test_reading_the_keeps_font_files(keep: Keep) -> None:
    (keep.dir / "fonts").mkdir()
    (keep.dir / "fonts" / "A.ttf").write_bytes(b"font")
    found = read_font_files(
        keep.dir, {"titles": "fonts/A.ttf", "notes": "fonts/gone.otf", "text": "Arial"}
    )
    assert found == {"fonts/A.ttf": b"font", "fonts/gone.otf": None}


def test_font_paths_stay_in_the_fonts_folder(keep: Keep) -> None:
    assert is_font_file("fonts/A.OTF")
    assert not is_font_file("Arial")
    assert not is_font_file("icons/A.ttf")
    assert font_path(keep.dir, "fonts/../keep.ttf") is None
    assert font_path(keep.dir, "fonts/A.ttf") == keep.dir / "fonts" / "A.ttf"
