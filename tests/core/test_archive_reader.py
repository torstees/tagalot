"""Reading the best image inside zip, 7z, and rar archives (#76)."""

import io
import zipfile
from pathlib import Path
from typing import Any

import py7zr
import pytest
import rarfile
from PIL import Image

from tagalot.core.thumbnails import archive
from tagalot.core.thumbnails.archive import (
    ArchiveError,
    ArchiveMember,
    archive_image_bytes,
    image_members,
    natural_key,
)
from tagalot.core.thumbnails.render import load_archive_image, renderer_for
from tagalot.themes.api import ResourceInfo
from tests.core.media_files import png_bytes


def _names(names: list[str]) -> list[str]:
    return [m.name for m in image_members([ArchiveMember(n, 10) for n in names])]


def _size(data: bytes | None) -> tuple[int, int]:
    assert data is not None
    return Image.open(io.BytesIO(data)).size


def write_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def write_7z(path: Path, members: dict[str, bytes], password: str | None = None) -> Path:
    with py7zr.SevenZipFile(path, "w", password=password) as sz:
        for name, data in members.items():
            sz.writestr(data, name)
    return path


# --- choosing ---


def test_natural_order() -> None:
    names = ["page10.jpg", "Page2.jpg", "page1.jpg", "page1b.jpg"]
    assert sorted(names, key=natural_key) == ["page1.jpg", "page1b.jpg", "Page2.jpg", "page10.jpg"]


def test_covers_and_previews_come_first() -> None:
    assert _names(["a/01.png", "a/thumb.png", "b/Preview_2.jpg", "cover.webp", "a/00.png"]) == [
        "cover.webp",
        "b/Preview_2.jpg",
        "a/thumb.png",
        "a/00.png",
        "a/01.png",
    ]


def test_only_real_images_are_candidates() -> None:
    members = [
        ArchiveMember("art/", 0, is_dir=True),
        ArchiveMember("__MACOSX/art/._cover.jpg", 10),
        ArchiveMember("art/.hidden.png", 10),
        ArchiveMember(".git/cover.png", 10),
        ArchiveMember("art/readme.txt", 10),
        ArchiveMember("art/model.blend", 10),
        ArchiveMember("art/sprite.PNG", 10),
    ]
    assert [m.name for m in image_members(members)] == ["art/sprite.PNG"]


# --- zip ---


def test_zip_reads_the_chosen_image(tmp_path: Path) -> None:
    path = write_zip(
        tmp_path / "pack.zip",
        {
            "pack/readme.txt": b"hi",
            "pack/sprites/a.png": png_bytes((10, 10)),
            "pack/cover.png": png_bytes((30, 20)),
        },
    )
    assert _size(archive_image_bytes(str(path))) == (30, 20)


def test_a_cbr_that_is_really_a_zip(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "comic.cbr", {"p1.jpg": png_bytes((12, 8))})
    assert _size(archive_image_bytes(str(path))) == (12, 8)


def test_an_archive_without_images(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "docs.zip", {"a.txt": b"x", "b/": b""})
    assert archive_image_bytes(str(path)) is None


def test_too_large_images_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    big, small = png_bytes((300, 300)), png_bytes((5, 5))
    monkeypatch.setattr(archive, "MAX_MEMBER_BYTES", len(small) + 10)
    path = write_zip(tmp_path / "p.zip", {"cover.png": big, "z.png": small})
    assert _size(archive_image_bytes(str(path))) == (5, 5)


def test_a_file_that_isnt_an_archive(tmp_path: Path) -> None:
    (tmp_path / "fake.zip").write_bytes(b"hello, not a zip")
    with pytest.raises(ArchiveError, match="not a zip, 7z, or rar"):
        archive_image_bytes(str(tmp_path / "fake.zip"))


def test_a_truncated_zip(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "p.zip", {"cover.png": png_bytes()})
    path.write_bytes(path.read_bytes()[:40])
    with pytest.raises(ArchiveError):
        archive_image_bytes(str(path))


# --- 7z ---


def test_7z_reads_the_chosen_image(tmp_path: Path) -> None:
    path = write_7z(
        tmp_path / "pack.7z",
        {"notes.txt": b"x" * 1000, "b.png": png_bytes((10, 10)), "a.png": png_bytes((20, 30))},
    )
    assert _size(archive_image_bytes(str(path))) == (20, 30)


def test_deep_in_a_solid_7z_is_given_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_7z(
        tmp_path / "solid.7z", {"big.bin": b"\0" * 5000, "cover.png": png_bytes((9, 9))}
    )
    monkeypatch.setattr(archive, "SOLID_READ_BUDGET", 1000)
    assert archive_image_bytes(str(path)) is None
    monkeypatch.setattr(archive, "SOLID_READ_BUDGET", 10_000)
    assert _size(archive_image_bytes(str(path))) == (9, 9)


def test_an_encrypted_7z_shows_nothing(tmp_path: Path) -> None:
    path = write_7z(tmp_path / "secret.7z", {"cover.png": png_bytes()}, password="hunter2")
    assert archive_image_bytes(str(path)) is None


# --- rar (no rar writer exists, so the reader is faked) ---


class FakeRarInfo:
    def __init__(self, name: str, size: int) -> None:
        self.filename, self.file_size = name, size

    def is_dir(self) -> bool:
        return self.filename.endswith("/")

    def needs_password(self) -> bool:
        return False


class FakeRar:
    data = png_bytes((14, 7))
    tool = True

    def __init__(self, path: str) -> None:
        self.path = path

    def is_solid(self) -> bool:
        return False

    def infolist(self) -> list[FakeRarInfo]:
        return [FakeRarInfo("art/", 0), FakeRarInfo("art/cover.jpg", len(self.data))]

    def open(self, name: str) -> Any:
        if not self.tool:
            raise rarfile.RarCannotExec("Cannot find working tool")
        return io.BytesIO(self.data)

    def close(self) -> None:
        pass


def _rar(tmp_path: Path) -> Path:
    path = tmp_path / "pack.rar"
    path.write_bytes(archive.RAR_MAGIC + b"\x00" * 20)
    return path


def test_rar_reads_the_chosen_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rarfile, "RarFile", FakeRar)
    assert _size(archive_image_bytes(str(_rar(tmp_path)))) == (14, 7)


def test_rar_without_a_tool_shows_nothing_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rarfile, "RarFile", FakeRar)
    monkeypatch.setattr(FakeRar, "tool", False)
    assert archive_image_bytes(str(_rar(tmp_path))) is None


# --- the renderer ---


def test_archives_render_through_the_archive_renderer(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "pack.cbz", {"001.png": png_bytes((40, 20))})
    resource = ResourceInfo(1, "r", "pack.cbz", "file", ".cbz", 1, 1, str(path))
    renderer = renderer_for(resource)
    assert renderer is not None
    assert renderer.id == "archive_image"
    image = renderer.load(str(path), 16)
    assert image is not None
    assert image.size == (40, 20)
    assert load_archive_image(str(write_zip(tmp_path / "e.zip", {"a.txt": b""})), 16) is None
