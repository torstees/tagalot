"""Tests for walking a root: excludes, extension filter, directories, errors."""

import os
from pathlib import Path

import pytest

from tagalot.core.models import ResourceKind
from tagalot.core.scanner import WalkEntry, compile_excludes, walk_root

TREE = [
    "Artist A/Album 1/01 Intro.flac",
    "Artist A/Album 1/02 Song.FLAC",
    "Artist A/Album 1/cover.jpg",
    "Artist A/Album 1/.DS_Store",
    "Artist A/Album 1/@eaDir/cover.jpg@SynoEAStream",
    "Artist A/notes.txt",
    "Björk/Homogenic/01 Hunter.flac",
    ".DS_Store",
    "Thumbs.db",
    "loose.mp3",
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for i, rel in enumerate(TREE):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (i + 1))
    return tmp_path


def _paths(entries: list[WalkEntry]) -> list[str]:
    return [e.relpath for e in entries]


def test_walks_every_file_with_posix_relpaths(root: Path) -> None:
    paths = _paths(list(walk_root(str(root))))
    assert sorted(paths) == sorted(TREE)
    assert all("\\" not in p for p in paths)


def test_order_is_deterministic(root: Path) -> None:
    assert _paths(list(walk_root(str(root)))) == _paths(list(walk_root(str(root))))


def test_file_entries_have_size_mtime_and_lowercase_ext(root: Path) -> None:
    entries = {e.relpath: e for e in walk_root(str(root))}
    song = entries["Artist A/Album 1/02 Song.FLAC"]
    st = (root / "Artist A/Album 1/02 Song.FLAC").stat()
    assert song == WalkEntry(
        relpath="Artist A/Album 1/02 Song.FLAC",
        kind=ResourceKind.FILE,
        ext=".flac",
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
    )
    assert entries[".DS_Store"].ext == ""  # a dotfile has no extension


def test_extension_filter_is_case_insensitive(root: Path) -> None:
    paths = _paths(list(walk_root(str(root), extensions={".flac", ".MP3"})))
    assert sorted(paths) == [
        "Artist A/Album 1/01 Intro.flac",
        "Artist A/Album 1/02 Song.FLAC",
        "Björk/Homogenic/01 Hunter.flac",
        "loose.mp3",
    ]


def test_design_example_excludes(root: Path) -> None:
    excludes = ["**/.DS_Store", "**/Thumbs.db", "**/@eaDir/**"]  # DESIGN.md §4
    paths = _paths(list(walk_root(str(root), exclude=excludes)))
    assert sorted(paths) == [
        "Artist A/Album 1/01 Intro.flac",
        "Artist A/Album 1/02 Song.FLAC",
        "Artist A/Album 1/cover.jpg",
        "Artist A/notes.txt",
        "Björk/Homogenic/01 Hunter.flac",
        "loose.mp3",
    ]


def test_excluded_folders_are_not_read(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    listed: list[str] = []
    real_scandir = os.scandir

    def spy(path: str) -> "os._ScandirIterator[str]":
        listed.append(Path(path).name)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", spy)
    list(walk_root(str(root), exclude=["**/@eaDir/**"]))
    assert "@eaDir" not in listed
    assert "Album 1" in listed


def test_directories_can_be_entries(root: Path) -> None:
    entries = list(walk_root(str(root), dirs=True, extensions={".flac"}))
    dirs = [e for e in entries if e.kind is ResourceKind.DIR]
    order = [d.relpath for d in dirs]
    assert all(order.index(p.rsplit("/", 1)[0]) < order.index(p) for p in order if "/" in p)
    assert sorted(order) == [
        "Artist A",
        "Artist A/Album 1",
        "Artist A/Album 1/@eaDir",
        "Björk",
        "Björk/Homogenic",
    ]
    assert all(d.size is None and d.ext == "" and d.mtime_ns > 0 for d in dirs)


def test_directory_predicate(root: Path) -> None:
    def albums_only(relpath: str) -> bool:
        return relpath.count("/") == 1  # Artist/Album

    entries = walk_root(str(root), dirs=albums_only, exclude=["**/@eaDir"])
    assert [e.relpath for e in entries if e.kind is ResourceKind.DIR] == [
        "Artist A/Album 1",
        "Björk/Homogenic",
    ]


def test_unreadable_folder_is_reported_and_skipped(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_scandir = os.scandir

    def flaky(path: str) -> "os._ScandirIterator[str]":
        if Path(path).name == "Album 1":
            raise PermissionError(13, "Access is denied", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky)
    errors: list[tuple[str, OSError]] = []
    paths = _paths(list(walk_root(str(root), on_error=lambda p, e: errors.append((p, e)))))
    assert [(p, type(e)) for p, e in errors] == [("Artist A/Album 1", PermissionError)]
    assert "Artist A/notes.txt" in paths
    assert "Björk/Homogenic/01 Hunter.flac" in paths
    assert not any(p.startswith("Artist A/Album 1/") for p in paths)


def test_symlinked_folders_are_not_followed(
    root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.flac").write_bytes(b"x")
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
        os.symlink(outside / "secret.flac", root / "linked.flac")
    except OSError:
        pytest.skip("creating symlinks needs extra privileges on this system")
    paths = _paths(list(walk_root(str(root), dirs=True)))
    assert "link" not in paths
    assert "link/secret.flac" not in paths
    assert "linked.flac" in paths  # symlinked files are included


def test_empty_root(tmp_path: Path) -> None:
    assert list(walk_root(str(tmp_path))) == []


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("**/.DS_Store", ".DS_Store", True),  # top level
        ("**/.DS_Store", "a/b/.DS_Store", True),
        ("**/.DS_Store", "a/.DS_Store.bak", False),
        ("**/thumbs.db", "Pics/Thumbs.db", True),  # case-insensitive
        ("**/@eaDir/**", "a/@eaDir", True),  # the folder itself, so it is pruned
        ("**/@eaDir/**", "a/@eaDir/x/y", True),
        ("**/@eaDir/**", "a/@eaDirX", False),
        ("*.tmp", "x.tmp", True),
        ("*.tmp", "a/x.tmp", False),  # * stays within one segment
        ("**/*.tmp", "a/x.tmp", True),
        ("Downloads/**", "Downloads/a/b.zip", True),
        ("Downloads/**", "My Downloads/a.zip", False),
        ("Downloads", "Downloads", True),
        ("Downloads", "Downloads/a.zip", False),
        ("a?c.txt", "abc.txt", True),
        ("a?c.txt", "a/c.txt", False),
        ("**/[0-9][0-9] *", "Album/01 Intro.flac", True),  # character ranges
        ("**/[!0-9]*.flac", "Album/01 Intro.flac", False),
        ("**/*(1).jpg", "a/b (1).jpg", True),  # regex metacharacters are literal
        ("  ", "anything", False),  # blank patterns are ignored
        ("Private\\**", "Private/x", True),  # backslashes accepted as separators
    ],
)
def test_exclude_globs(pattern: str, path: str, matches: bool) -> None:
    regex = compile_excludes([pattern])
    assert (regex is not None and regex.match(path) is not None) is matches
