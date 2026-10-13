"""Galleries, level 1 (#386): a role's pictures in groups, an archive's pictures listed and
drawn within the archive reader's budgets, and one opened from a temporary copy."""

import os
from datetime import timedelta
from pathlib import Path

import pytest
import rarfile

from tagalot.core import gallery
from tagalot.core.detail import PATH_MARK, FileRow
from tagalot.core.gallery import (
    ArchivePicture,
    extract_member,
    gallery_groups,
    list_archive,
)
from tagalot.core.models import ResourceStatus
from tagalot.core.thumbnails import archive
from tagalot.core.thumbnails.cache import ThumbCache
from tests.core.media_files import png_bytes
from tests.core.test_archive_reader import FakeRar, _rar, write_7z, write_zip
from tests.core.test_thumb_resolve import T0
from tests.core.test_thumb_resolve import Env as ThumbEnv
from tests.core.test_thumb_resolve import env as thumb_env

__all__ = ["thumb_env"]  # fixtures

PNG = png_bytes((40, 20))


def _row(relpath: str, resource_id: int = 1, root_id: str = "r") -> FileRow:
    return FileRow(
        resource_id, "Files", relpath, "file", 1, ResourceStatus.OK, f"/x/{relpath}",
        root_id=root_id,
    )  # fmt: skip


def _shape(rows: list[FileRow]) -> list[tuple[str, str, list[str]]]:
    return [
        (g.kind, g.title, [f.relpath.rpartition("/")[2] for f in g.files])
        for g in gallery_groups(rows)
    ]


# --- groups ---


def test_loose_pictures_then_subfolders_then_archives() -> None:
    rows = [
        _row("Movie/Screens/Disc 10/b.jpg"),
        _row("Movie/Screens/still 10.jpg"),
        _row("Movie/Screens/Extras.zip"),
        _row("Movie/Screens/Disc 2/a.jpg"),
        _row("Movie/Screens/still 2.jpg"),
        _row("Movie/Screens/Behind.cbz"),
        _row("Movie/Screens/Disc 2/Deleted/c.jpg"),
    ]
    assert _shape(rows) == [
        ("loose", "", ["still 2.jpg", "still 10.jpg"]),  # natural order
        ("folder", "Disc 2", ["a.jpg"]),
        ("folder", "Disc 2/Deleted", ["c.jpg"]),
        ("folder", "Disc 10", ["b.jpg"]),
        ("archive", "Behind.cbz", ["Behind.cbz"]),
        ("archive", "Extras.zip", ["Extras.zip"]),
    ]


def test_one_folder_is_all_loose() -> None:
    rows = [_row("Movie/Screens/Disc 2/b.jpg"), _row("Movie/Screens/Disc 2/a.jpg")]
    assert _shape(rows) == [("loose", "", ["a.jpg", "b.jpg"])]
    assert gallery_groups([]) == []


def test_files_in_two_roots_group_by_their_folders() -> None:
    rows = [_row("A/x.jpg", 1, "r1"), _row("B/y.jpg", 2, "r2")]
    assert _shape(rows) == [("folder", "A", ["x.jpg"]), ("folder", "B", ["y.jpg"])]


# --- an archive's pictures ---


def test_an_archives_pictures_are_listed_in_natural_order(tmp_path: Path) -> None:
    path = write_zip(
        tmp_path / "Extras.zip",
        {
            "shots/page10.png": PNG,
            "shots/page2.png": PNG,
            "cover.jpg": PNG,
            "notes.txt": b"no",
            "__MACOSX/._cover.jpg": b"",
            ".hidden.png": PNG,
            "inner/more.zip": b"PK\x05\x06" + b"\x00" * 18,
            "shots/": b"",
        },
    )
    listing = list_archive(str(path))
    assert listing.error is None
    assert [(p.name, p.nested) for p in listing.pictures] == [
        ("cover.jpg", False),
        ("inner/more.zip", True),  # an archive inside: listed, not opened
        ("shots/page2.png", False),
        ("shots/page10.png", False),
    ]
    assert listing.more == 0


def test_a_long_archive_lists_the_first_and_counts_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gallery, "MAX_LISTED", 3)
    path = write_zip(tmp_path / "a.zip", {f"{n}.png": PNG for n in range(1, 6)})
    listing = list_archive(str(path))
    assert [p.name for p in listing.pictures] == ["1.png", "2.png", "3.png"]
    assert listing.more == 2


def test_budgets_mark_pictures_too_big_or_too_deep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gallery, "MAX_MEMBER_BYTES", 100)
    big = write_zip(tmp_path / "big.zip", {"small.png": PNG[:90], "large.png": PNG + b"x" * 200})
    readable = {p.name: p.readable for p in list_archive(str(big)).pictures}
    assert readable == {"large.png": False, "small.png": True}

    monkeypatch.setattr(gallery, "MAX_MEMBER_BYTES", archive.MAX_MEMBER_BYTES)
    monkeypatch.setattr(gallery, "SOLID_READ_BUDGET", len(PNG))
    solid = write_7z(tmp_path / "solid.7z", {"1.png": PNG, "2.png": PNG, "3.png": PNG})
    listing = list_archive(str(solid))
    assert listing.solid
    assert [p.readable for p in listing.pictures] == [True, True, False]  # 2 * PNG before 3


def test_a_damaged_archive_says_why(tmp_path: Path) -> None:
    path = tmp_path / "broken.zip"
    path.write_bytes(b"PK\x03\x04 this is not really a zip")
    listing = list_archive(str(path))
    assert listing.pictures == ()
    assert listing.error


def test_rar_lists_with_a_tool_and_is_one_picture_without(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rarfile, "RarFile", FakeRar)
    monkeypatch.setattr(gallery, "rar_tool", lambda: True)
    listing = list_archive(str(_rar(tmp_path)))
    assert listing.pictures == (ArchivePicture("art/cover.jpg", len(FakeRar.data)),)
    monkeypatch.setattr(gallery, "rar_tool", lambda: False)
    assert list_archive(str(_rar(tmp_path))).no_tool


# --- thumbnails of an archive's pictures ---


def test_thumbnails_are_cached_by_archive_member_and_its_time(
    thumb_env: ThumbEnv, tmp_path: Path
) -> None:
    env = thumb_env
    zipped = write_zip(env.files / "Blue Train/scans.zip", {"a.png": PNG, "b.png": b"not png"})
    env.scan(T0 + timedelta(hours=1))
    scans = env.resource("Blue Train/scans.zip")
    problems: list[tuple[str, str]] = []
    resolver = env.resolver()
    resolver.report = problems.extend
    found = dict(resolver.archive_thumbnails(scans, ["a.png", "b.png", "gone.png"]))
    assert found["a.png"] is not None
    assert (found["a.png"].width, found["a.png"].height) == (40, 20)
    assert found["b.png"] is None
    assert found["gone.png"] is None
    assert [where for where, _ in problems] == [f"{zipped} {PATH_MARK} b.png"]  # reported

    os.remove(zipped)
    kept, cache = env.cache, ThumbCache(tmp_path / "other-thumbs.db")
    try:
        env.cache = cache  # an empty cache must open the archive, which is gone
        assert dict(env.resolver().archive_thumbnails(scans, ["a.png"]))["a.png"] is None
    finally:
        env.cache = kept
        cache.close()
    reread = dict(env.resolver().archive_thumbnails(scans, ["a.png"]))
    assert reread["a.png"] is not None  # from the cache, though the archive is gone


def test_a_damaged_archive_is_reported_not_raised(thumb_env: ThumbEnv) -> None:
    env = thumb_env
    broken = env.files / "Blue Train/broken.zip"
    broken.write_bytes(b"PK\x03\x04 not a zip")
    env.scan(T0 + timedelta(hours=1))
    problems: list[tuple[str, str]] = []
    resolver = env.resolver()
    resolver.report = problems.extend
    assert resolver.archive_thumbnails(env.resource("Blue Train/broken.zip"), ["a.png"]) == [
        ("a.png", None)
    ]
    assert [where for where, _ in problems] == [str(broken)]


# --- opening ---


def test_opening_extracts_a_copy_and_never_touches_the_archive(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "Extras.zip", {"shots/a.png": PNG, "../escape.png": PNG})
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    folder = tmp_path / "temp" / "archives" / "7"
    copy = extract_member(str(path), "shots/a.png", folder)
    assert copy == folder / "shots" / "a.png"
    assert copy.read_bytes() == PNG
    escaped = extract_member(str(path), "../escape.png", folder)
    assert escaped == folder / "escape.png"  # stays inside the folder
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    with pytest.raises(archive.ArchiveError, match="isn't in the archive"):
        extract_member(str(path), "nope.png", folder)
