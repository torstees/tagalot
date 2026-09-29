"""assets2d over a realistic asset library (#84): every kind of file the theme accepts, awkward
names, deep folders, operating-system leftovers, and damaged files. Everything is generated
here (images with Pillow, PSD and WOFF from bytes, archives with zipfile and py7zr)."""

import zipfile
from pathlib import Path

import py7zr
import pytest

from tagalot.builtin_themes.assets2d import Archive, Artist, Assets2DTheme, Font, Image
from tagalot.core.keep import DEFAULT_EXCLUDES, RootConfig
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.thumbnails.resolve import ThumbnailResolver, ThumbnailResult
from tests.core.media_files import font_bytes, png_bytes, psd_bytes, woff_bytes, write_image
from tests.themes.test_assets2d import T0, Env, env

__all__ = ["env"]  # fixture


def _zip(path: Path, members: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """Replaces test_assets2d's library: the env fixture scans whatever this returns."""
    files = tmp_path / "files"
    studio = files / "Ærø Studio"  # a non-ASCII artist
    write_image(studio / "Deep/Er/Still/banner.PNG", (400, 100))  # upper-case extension
    (studio / "poster.psd").write_bytes(psd_bytes((60, 90), (20, 120, 200)))
    (studio / "Type").mkdir()
    (studio / "Type/Aileron.woff").write_bytes(woff_bytes(font_bytes()))
    (studio / "Type/Aileron.ttf").write_bytes(font_bytes())
    _zip(studio / "comic.cbz", {f"{n:03}.png": png_bytes((30 + n, 40)) for n in (10, 2, 1)})
    with py7zr.SevenZipFile(studio / "sprites.7z", "w") as sz:
        sz.writestr(png_bytes((16, 16)), "sprites/thumb.png")
        sz.writestr(png_bytes((64, 64)), "sprites/sheet.png")
    _zip(studio / "empty.zip", {"readme.txt": b"no pictures here"})
    (studio / "damaged.zip").write_bytes(b"PK\x03\x04 but then nothing sensible")
    (studio / "fake.ttf").write_bytes(b"not a font")
    # Operating-system leftovers the default exclude patterns skip.
    (studio / "._poster.psd").write_bytes(b"\x00\x05\x16\x07AppleDouble")
    (studio / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (studio / "Thumbs.db").write_bytes(b"\xd0\xcf\x11\xe0")
    write_image(files / "Solo/a b  c (final) [v2].jpg", (10, 20))  # spaces and brackets
    (files / "Solo/notes.txt").write_text("not an asset", encoding="utf-8")
    return files


@pytest.fixture
def scanned(env: Env) -> tuple[Env, ScanReport]:
    root = RootConfig("r", "Assets", str(env.files), list(DEFAULT_EXCLUDES))
    report = scan_root(
        env.writer, env.reader, root, root.path, when=T0, theme=Assets2DTheme, schema=env.schema
    )
    return env, report


def _resolve(env: Env, title: str) -> ThumbnailResult:
    resolver = ThumbnailResolver(
        env.reader, env.writer, Assets2DTheme, env.cache, lambda _: str(env.files), 128
    )
    return resolver.resolve(env.entity(title))


def test_every_file_lands_in_the_right_type(scanned: tuple[Env, ScanReport]) -> None:
    env, report = scanned
    assert report.ingest_errors == []
    assert env.titles(Artist) == ["Solo", "Ærø Studio"]
    assert env.titles(Image) == ["a b  c (final) [v2].jpg", "banner.PNG", "poster.psd"]
    assert env.titles(Font) == ["Aileron.ttf", "Aileron.woff", "fake.ttf"]
    assert env.titles(Archive) == ["comic.cbz", "damaged.zip", "empty.zip", "sprites.7z"]
    # Nothing from ._poster.psd, .DS_Store, Thumbs.db, or notes.txt.


def test_deep_files_belong_to_the_top_folders_artist(scanned: tuple[Env, ScanReport]) -> None:
    env, _ = scanned
    banner = env.fields(Image, "banner.PNG")
    assert (banner["artist"], banner["extension"]) == ("Ærø Studio", ".png")
    assert banner["folder"] == "Ærø Studio/Deep/Er/Still"
    assert len(env.artists()["Ærø Studio"]) == 9


def test_details_for_each_format(scanned: tuple[Env, ScanReport]) -> None:
    env, _ = scanned
    assert env.fields(Image, "poster.psd")["dimensions"] == "60 \u00d7 90"
    assert env.fields(Image, "banner.PNG")["width"] == 400
    for name in ("Aileron.woff", "Aileron.ttf"):
        font = env.fields(Font, name)
        assert (font["family"], font["style"]) == ("Aileron", "Regular"), name
    assert env.fields(Archive, "comic.cbz")["images"] == 3
    assert env.fields(Archive, "sprites.7z")["images"] == 2
    assert env.fields(Archive, "empty.zip")["images"] == 0


def test_damaged_files_are_assets_with_warnings(scanned: tuple[Env, ScanReport]) -> None:
    env, report = scanned
    warned = sorted(w.relpath or "" for w in report.ingest_warnings)
    assert warned == ["Ærø Studio/damaged.zip", "Ærø Studio/fake.ttf"]
    assert env.fields(Archive, "damaged.zip")["images"] is None
    assert env.fields(Font, "fake.ttf")["family"] is None


@pytest.mark.parametrize(
    ("title", "size"),
    [
        ("poster.psd", (60, 90)),  # the PSD's composite image (never enlarged)
        ("Aileron.woff", (128, 128)),  # a font sample, from WOFF as well as TTF
        ("comic.cbz", (31, 40)),  # 001.png: natural order, not 010.png
        ("sprites.7z", (16, 16)),  # "thumb" in the name wins
        ("banner.PNG", (128, 32)),
    ],
)
def test_thumbnails_for_each_format(
    scanned: tuple[Env, ScanReport], title: str, size: tuple[int, int]
) -> None:
    env, _ = scanned
    result = _resolve(env, title)
    assert result.thumbnail is not None, result
    assert (result.thumbnail.width, result.thumbnail.height) == size


@pytest.mark.parametrize(
    ("title", "icon"),
    [("empty.zip", "archive"), ("damaged.zip", "archive"), ("fake.ttf", "font")],
)
def test_files_without_a_picture_show_their_icon(
    scanned: tuple[Env, ScanReport], title: str, icon: str
) -> None:
    env, _ = scanned
    result = _resolve(env, title)
    assert (result.thumbnail, result.icon) == (None, icon)
