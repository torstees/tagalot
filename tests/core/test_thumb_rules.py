"""Thumbnail rules, table-driven (#79): which archive member is chosen, which folder images
count, and when a cached thumbnail is replaced (DESIGN.md §10)."""

import io
import os
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from tagalot.core.thumbnails.archive import ArchiveMember, archive_image_bytes, image_members
from tagalot.themes.api import EntityRef, FolderImage
from tests.core.media_files import png_bytes, write_image
from tests.core.test_thumb_providers import FakeContext, _file
from tests.core.test_thumb_resolve import T0, Env, env, picture_size

__all__ = ["env"]  # fixture

# --- archive member selection ---


@pytest.mark.parametrize(
    ("names", "chosen"),
    [
        # A preferred word wins, in the order cover, preview, thumb.
        (["01.png", "thumb.png", "preview.png", "cover.png"], "cover.png"),
        (["01.png", "thumb.png", "preview.png"], "preview.png"),
        (["01.png", "thumbnail.jpg"], "thumbnail.jpg"),
        # Only the file name counts, not its folder.
        (["covers/02.png", "01.png"], "01.png"),
        (["art/Front COVER.JPG", "art/00.jpg"], "art/Front COVER.JPG"),
        # Among equals, natural order: page2 before page10, case-insensitively.
        (["page10.jpg", "page2.jpg", "Page1.jpg"], "Page1.jpg"),
        (["b/cover2.png", "a/cover10.png", "a/cover9.png"], "a/cover9.png"),
        # Folders sort as part of the path.
        (["z/01.png", "a/99.png"], "a/99.png"),
        # Skipped: folders, dotfiles and dot folders, macOS metadata, non-images.
        (["__MACOSX/._cover.png", "real.png"], "real.png"),
        ([".cover.png", ".git/cover.png", "x.png"], "x.png"),
        (["cover.txt", "cover.psd.bak", "notes.md", "y.webp"], "y.webp"),
        # Any image extension, in any case.
        (["scan.TIFF"], "scan.TIFF"),
        (["a.gif", "b.bmp"], "a.gif"),
        # Nothing to choose.
        (["readme.txt", "docs/"], None),
        ([], None),
    ],
)
def test_archive_member_choice(names: list[str], chosen: str | None) -> None:
    members = [ArchiveMember(n, 10, is_dir=n.endswith("/")) for n in names]
    ranked = image_members(members)
    assert (ranked[0].name if ranked else None) == chosen


def test_only_the_chosen_member_is_read(tmp_path: Path) -> None:
    """A broken member elsewhere in the zip doesn't matter: only the choice is read."""
    path = tmp_path / "pack.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("01.png", b"not really a png")
        zf.writestr("cover.png", png_bytes((11, 7)))
    data = archive_image_bytes(str(path))
    assert data is not None
    assert Image.open(io.BytesIO(data)).size == (11, 7)


# --- folder conventions ---


@pytest.mark.parametrize(
    ("names", "found"),
    [
        (["folder.jpg"], ["folder.jpg"]),
        (["FOLDER.JPG", "Cover.Jpeg", "front.PNG", "AlbumArt.webp"], None),  # all, in order
        (["AlbumArt_{7D2A0E1B}_Large.jpg"], ["AlbumArt_{7D2A0E1B}_Large.jpg"]),
        (["AlbumArt_{7D2A0E1B}_Small.jpg"], []),  # only the large one
        (["AlbumArtSmall.jpg"], ["AlbumArtSmall.jpg"]),
        (["cover (1).jpg", "my cover.jpg", "coverart.jpg"], []),  # whole names only
        (["folder.gif", "folder.bmp", "folder.tiff"], []),  # only jpg, jpeg, png, webp
        (["folder.jpg.bak", "folder"], []),
        (["back.jpg", "scan01.jpg"], []),
    ],
)
def test_folder_image_names(names: list[str], found: list[str] | None) -> None:
    files = [_file(f"Album/{n}", i) for i, n in enumerate(names)]
    # The context narrows by extension, as the database query does.
    wanted = {".jpg", ".jpeg", ".png", ".webp"}
    ctx = FakeContext([f for f in files if f.ext in wanted], {}, [])
    chosen = [
        r.relpath.rpartition("/")[2] for r in FolderImage().candidates(EntityRef(1, "a"), ctx)
    ]
    assert chosen == (names if found is None else found)


def test_folder_images_follow_the_order_of_preference() -> None:
    names = ["albumartsmall.jpg", "AlbumArt_{X}_Large.jpg", "albumart.png", "front.jpg"]
    names += ["cover.jpg", "folder.png"]
    ctx = FakeContext([_file(f"a/{n}", i) for i, n in enumerate(sorted(names))], {}, [])
    chosen = [r.relpath[2:] for r in FolderImage().candidates(EntityRef(1, "a"), ctx)]
    assert chosen == list(reversed(names))


def test_extra_folder_names_extend_the_list() -> None:
    ctx = FakeContext([_file("a/poster.jpg", 1), _file("a/folder.jpg", 2)], {}, [])
    provider = FolderImage(["poster", "folder"])
    assert [r.id for r in provider.candidates(EntityRef(1, "a"), ctx)] == [1, 2]


# --- cache invalidation ---


def _touch(path: Path, mtime_ns: int) -> None:
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _png(color: str, length: int = 400) -> bytes:
    """A 200x100 PNG padded to ``length`` bytes (readers ignore what follows its end)."""
    data = png_bytes((200, 100), color)
    assert len(data) <= length
    return data + bytes(length - len(data))


def test_same_size_new_content_is_told_apart_by_its_time(env: Env) -> None:
    path = env.files / "beach.png"
    path.write_bytes(_png("red"))
    env.scan(T0 + timedelta(hours=1))
    photo = env.entity("beach.png")
    env.resolver().resolve(photo)
    path.write_bytes(_png("lime"))  # the same size: only the modification time differs
    _touch(path, 1_900_000_000_000_000_000)
    env.scan(T0 + timedelta(hours=2))
    second = env.resolver().resolve(photo).thumbnail
    assert second is not None
    red, green, _ = Image.open(io.BytesIO(second.data)).convert("RGB").getpixel((5, 5))  # type: ignore[misc]
    assert green > 200 > red  # the new picture
    assert env.cache.stats().count == 2  # a new key; the old entry is simply never read


@pytest.mark.parametrize(
    ("change", "remade"),
    [
        ("nothing", False),
        ("touch", True),  # a new modification time, even with the same bytes
        ("grow", True),  # a new size
    ],
)
def test_what_invalidates_a_cached_thumbnail(env: Env, change: str, remade: bool) -> None:
    photo = env.entity("beach.png")
    env.resolver().resolve(photo)
    path = env.files / "beach.png"
    if change == "touch":
        _touch(path, 1_900_000_000_000_000_000)
    elif change == "grow":
        stat = path.stat()
        with path.open("ab") as f:
            f.write(b"\0" * 16)  # trailing bytes: still a readable PNG
        _touch(path, stat.st_mtime_ns)
    env.scan(T0 + timedelta(hours=1))
    env.resolver().resolve(photo)
    assert env.cache.stats().count == (2 if remade else 1)


def test_a_changed_file_is_not_remade_before_a_scan_notices(env: Env) -> None:
    """Keys use the size and time the database recorded, so an edited file keeps its old
    thumbnail until a scan sees the change (and no file is read to find out)."""
    photo = env.entity("beach.png")
    env.resolver().resolve(photo)
    write_image(env.files / "beach.png", (100, 200))
    _touch(env.files / "beach.png", 1_900_000_000_000_000_000)
    assert picture_size(env.resolver().resolve(photo)) == (64, 32)
    env.scan(T0 + timedelta(hours=1))
    assert picture_size(env.resolver().resolve(photo)) == (32, 64)


def test_an_archive_whose_cover_changes(env: Env) -> None:
    path = env.files / "pack.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("cover.png", png_bytes((40, 20)))
    env.scan(T0 + timedelta(hours=1))
    pack = env.entity("pack.zip")
    assert picture_size(env.resolver().resolve(pack)) == (40, 20)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("cover.png", png_bytes((20, 40)))
    _touch(path, 1_900_000_000_000_000_000)
    env.scan(T0 + timedelta(hours=2))
    assert picture_size(env.resolver().resolve(pack)) == (20, 40)
