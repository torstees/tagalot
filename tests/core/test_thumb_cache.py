"""Tests for making thumbnails and the thumbs.db cache (DESIGN.md §10, #73)."""

import io
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image, features

from tagalot.core.thumbnails.cache import CACHE_VERSION, ThumbCache, thumb_key
from tagalot.core.thumbnails.image import Thumbnail, make_thumbnail


def _decode(thumbnail: Thumbnail) -> Image.Image:
    decoded = Image.open(io.BytesIO(thumbnail.data))
    decoded.load()
    return decoded


# --- making thumbnails ---


@pytest.mark.parametrize(
    ("size", "box", "expected"),
    [
        ((800, 600), 256, (256, 192)),
        ((600, 800), 256, (192, 256)),
        ((100, 50), 256, (100, 50)),  # never enlarged
        ((5000, 10), 256, (256, 1)),  # at least one pixel
    ],
)
def test_thumbnails_fit_the_box(size: tuple[int, int], box: int, expected: tuple[int, int]) -> None:
    thumbnail = make_thumbnail(Image.new("RGB", size, (200, 30, 30)), box)
    assert (thumbnail.width, thumbnail.height) == expected
    decoded = _decode(thumbnail)
    assert decoded.size == expected
    assert decoded.format == "WEBP"
    assert thumbnail.format == "webp"


def test_exif_orientation_is_applied() -> None:
    source = Image.new("RGB", (400, 200), (10, 120, 200))
    exif = source.getexif()
    exif[0x0112] = 6  # "rotate 90° clockwise to display"
    buffer = io.BytesIO()
    source.save(buffer, "JPEG", exif=exif)
    thumbnail = make_thumbnail(Image.open(io.BytesIO(buffer.getvalue())), 100)
    assert (thumbnail.width, thumbnail.height) == (50, 100)  # upright: taller than wide


@pytest.mark.parametrize(
    ("mode", "alpha"),
    [("RGBA", True), ("LA", True), ("CMYK", False), ("L", False), ("I;16", False), ("P", False)],
)
def test_modes_become_rgb_or_rgba(mode: str, alpha: bool) -> None:
    source = Image.new(mode, (64, 64))
    decoded = _decode(make_thumbnail(source, 32))
    assert decoded.mode == ("RGBA" if alpha else "RGB")


def test_transparency_is_kept() -> None:
    source = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    source.paste((255, 0, 0, 255), (0, 0, 32, 64))
    decoded = _decode(make_thumbnail(source, 64))
    assert decoded.getchannel("A").getpixel((50, 10)) == 0  # still transparent
    assert decoded.getchannel("A").getpixel((10, 10)) == 255


def test_png_when_webp_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(features, "check", lambda feature: False)
    thumbnail = make_thumbnail(Image.new("RGB", (80, 40)), 40)
    assert thumbnail.format == "png"
    assert _decode(thumbnail).format == "PNG"


def test_a_bad_size_is_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        make_thumbnail(Image.new("RGB", (10, 10)), 0)


# --- keys ---


def test_keys_change_with_everything_a_thumbnail_depends_on() -> None:
    base = (7, 1234, 1_700_000_000_000_000_000, "image_file", 1, 256)
    key = thumb_key(*base)
    assert thumb_key(*base) == key  # stable
    for i, other in enumerate([8, 1235, 1_700_000_000_000_000_001, "folder_image", 2, 128]):
        changed = list(base)
        changed[i] = other
        assert thumb_key(*changed) != key, f"field {i} doesn't change the key"  # type: ignore[arg-type]
    assert len(key) == 64


# --- the cache ---


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[ThumbCache]:
    cache = ThumbCache(tmp_path / "thumbs.db")
    yield cache
    cache.close()


def _thumb(color: tuple[int, int, int] = (1, 2, 3)) -> Thumbnail:
    return make_thumbnail(Image.new("RGB", (40, 20), color), 40)


def test_get_put_and_replace(cache: ThumbCache) -> None:
    assert cache.get("k") is None
    first, second = _thumb((255, 0, 0)), _thumb((0, 0, 255))
    cache.put("k", 40, first)
    assert cache.get("k") == first
    cache.put("k", 40, second)
    assert cache.get("k") == second
    assert cache.stats().count == 1


def test_clear_and_stats(cache: ThumbCache) -> None:
    for i in range(5):
        cache.put(f"k{i}", 40, _thumb())
    stats = cache.stats()
    assert stats.count == 5
    assert stats.bytes == 5 * len(_thumb().data)
    assert cache.clear() == 5
    assert cache.stats().count == 0
    assert cache.get("k0") is None


def test_the_file_is_created_on_first_use_and_can_be_deleted(tmp_path: Path) -> None:
    path = tmp_path / "thumbs.db"
    cache = ThumbCache(path)
    assert not path.exists()
    cache.put("k", 40, _thumb())
    assert path.exists()
    cache.close()
    path.unlink()  # "The entire file can be deleted at any time."
    again = ThumbCache(path)
    assert again.get("k") is None
    again.put("k", 40, _thumb())
    again.close()


def test_a_damaged_file_starts_afresh(tmp_path: Path) -> None:
    path = tmp_path / "thumbs.db"
    path.write_bytes(b"this is not a database" * 100)
    cache = ThumbCache(path)
    assert cache.get("k") is None
    cache.put("k", 40, _thumb())
    assert cache.get("k") is not None
    cache.close()


def test_another_format_version_starts_afresh(tmp_path: Path) -> None:
    path = tmp_path / "thumbs.db"
    cache = ThumbCache(path)
    cache.put("k", 40, _thumb())
    cache.close()
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {CACHE_VERSION + 1}")
    raw.commit()
    raw.close()
    reopened = ThumbCache(path)
    assert reopened.get("k") is None  # its old contents were dropped
    assert reopened.stats().count == 0
    reopened.close()


def test_threads_can_share_it(cache: ThumbCache) -> None:
    thumbnail = _thumb()
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(20):
                cache.put(f"t{n}-{i}", 40, thumbnail)
                assert cache.get(f"t{n}-{i}") == thumbnail
        except BaseException as e:  # reported below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert cache.stats().count == 120
