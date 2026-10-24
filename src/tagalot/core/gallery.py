"""Galleries on an item's page (DESIGN.md §9 *Galleries*, level 1, #386).

A gallery role's files are shown in groups (:func:`gallery_groups`): loose pictures first
(those directly in the folder every file of the role shares), then one group per subfolder
under it (headed by its path), then one group per archive (headed by its file name), each
in natural name order.

An archive's pictures are listed when the page opens (:func:`list_archive`, in a worker)
with the thumbnails' archive reader (``core/thumbnails/archive.py``) and its budgets. They
aren't resources: they can't be tagged or linked. An archive inside an archive isn't
opened; it is listed as one entry. A rar archive without a tool to read it is shown as one
picture, as before. Opening a picture from an archive extracts it to this session's
temporary folder (:func:`extract_member`) and opens that copy; the archive is never
touched.
"""

import logging
import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import rarfile

from tagalot.core.detail import FileRow
from tagalot.core.thumbnails.archive import (
    MAX_MEMBER_BYTES,
    SOLID_READ_BUDGET,
    ArchiveError,
    ArchiveMember,
    is_candidate_image,
    natural_key,
    open_archive,
    read_member,
)
from tagalot.themes.api import KIND_EXTENSIONS, Kind

logger = logging.getLogger(__name__)

ARCHIVE_EXTENSIONS = KIND_EXTENSIONS[Kind.ARCHIVE]
MAX_LISTED = 1000
"""Pictures listed per archive; the rest are counted ("and 12 more")."""
MAX_OPENED_BYTES = 256 * 1024 * 1024
"""The largest member that opening extracts (a picture, or an archive inside the archive)."""


@dataclass(frozen=True)
class GalleryGroup:
    """One block of a gallery."""

    kind: Literal["loose", "folder", "archive"]
    title: str
    """The subfolder's path under the gallery's folder, or the archive's file name; ``""``
    for loose pictures."""
    files: tuple[FileRow, ...] = ()
    """The pictures (loose or folder), or the archive itself (one row)."""


def _name(relpath: str) -> str:
    return relpath.rpartition("/")[2]


def is_archive(file: FileRow) -> bool:
    return PurePosixPath(file.relpath).suffix.lower() in ARCHIVE_EXTENSIONS


def gallery_groups(files: Sequence[FileRow]) -> list[GalleryGroup]:
    """A gallery role's files in groups: loose pictures, then subfolders, then archives."""
    folders = [posixpath.dirname(f.relpath) for f in files]
    base = posixpath.commonpath(folders) if folders and len({f.root_id for f in files}) <= 1 else ""
    by_folder: dict[str, list[FileRow]] = {}
    archives: list[FileRow] = []
    for file, folder in zip(files, folders, strict=True):
        if is_archive(file):
            archives.append(file)
            continue
        under = posixpath.relpath(folder, base) if base else folder
        by_folder.setdefault("" if under == "." else under, []).append(file)
    groups = []
    loose = by_folder.pop("", [])
    if loose:
        groups.append(GalleryGroup("loose", "", _sorted(loose)))
    for title in sorted(by_folder, key=natural_key):
        groups.append(GalleryGroup("folder", title, _sorted(by_folder[title])))
    for archive in sorted(archives, key=lambda f: natural_key(_name(f.relpath))):
        groups.append(GalleryGroup("archive", _name(archive.relpath), (archive,)))
    return groups


def _sorted(files: list[FileRow]) -> tuple[FileRow, ...]:
    return tuple(sorted(files, key=lambda f: natural_key(_name(f.relpath))))


# --- an archive's pictures ---


@dataclass(frozen=True)
class ArchivePicture:
    """A picture (or an archive) inside an archive, as listed."""

    name: str
    """Its path inside the archive, with ``/`` separators."""
    size: int
    nested: bool = False
    """An archive inside the archive: listed, not opened."""
    readable: bool = True
    """Within the budgets for a thumbnail (not encrypted, not too big, not too deep in a
    solid archive)."""


@dataclass(frozen=True)
class ArchiveListing:
    """What :func:`list_archive` found."""

    pictures: tuple[ArchivePicture, ...] = ()
    more: int = 0
    """Pictures past :data:`MAX_LISTED`."""
    error: str | None = None
    """Why the archive couldn't be read (damaged, not an archive)."""
    no_tool: bool = False
    """A rar archive with no tool installed to read it: shown as one picture."""
    solid: bool = False


def _picture(member: ArchiveMember, before: int, solid: bool) -> ArchivePicture | None:
    suffix = PurePosixPath(member.name).suffix.lower()
    if not member.is_dir and suffix in ARCHIVE_EXTENSIONS:
        hidden = any(p.startswith(".") or p == "__MACOSX" for p in PurePosixPath(member.name).parts)
        return None if hidden else ArchivePicture(member.name, member.size, nested=True)
    if not is_candidate_image(member):
        return None
    readable = not member.encrypted and member.size <= MAX_MEMBER_BYTES
    if solid and before > SOLID_READ_BUDGET:
        readable = False
    return ArchivePicture(member.name, member.size, readable=readable)


def list_archive(path: str) -> ArchiveListing:
    """The pictures (and archives) inside the archive at ``path``, in natural name order.
    Never raises for a bad archive: the listing says what went wrong."""
    try:
        with open_archive(path) as archive:
            if archive.format == "rar" and not rar_tool():
                return ArchiveListing(no_tool=True)
            members = archive.members()
            solid = archive.solid
    except (ArchiveError, OSError, rarfile.Error) as e:
        logger.warning("Can't list %s: %s", path, e)
        return ArchiveListing(error=str(e) or type(e).__name__)
    found, before = [], 0
    for member in members:
        picture = _picture(member, before, solid)
        before += member.size
        if picture is not None:
            found.append(picture)
    found.sort(key=lambda p: natural_key(p.name))
    listed = tuple(found[:MAX_LISTED])
    return ArchiveListing(listed, len(found) - len(listed), solid=solid)


def rar_tool() -> bool:
    """Whether a tool to read compressed rar members is installed."""
    try:
        rarfile.tool_setup()
    except rarfile.RarCannotExec:
        return False
    return True


_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]')


def extract_member(path: str, name: str, folder: Path) -> Path:
    """Copy the member ``name`` of the archive at ``path`` into ``folder`` (a folder of this
    archive's own in the session's temporary folder), keeping its path inside the archive,
    and return the copy's path. The archive is only read. Raises :class:`ArchiveError` or
    ``OSError``."""
    data = read_member(path, name, MAX_OPENED_BYTES)
    parts = [_UNSAFE.sub("_", p) for p in PurePosixPath(name).parts if p not in ("", ".", "..")]
    if not parts:
        raise ArchiveError(f"{name!r} isn't a file name")
    where = folder.joinpath(*parts)
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_bytes(data)
    return where
