"""Reading the best image inside an archive (DESIGN.md §10 "Archives").

:class:`ArchiveReader` lists and reads members of zip, 7z, and rar archives (rar needs an
external tool such as ``unrar``, 7-Zip, or ``bsdtar`` for compressed members; without one,
rar archives simply show no picture). The format is recognized by content, so a ``.cbr``
that is really a zip still works.

:func:`archive_image_bytes` picks one image (a cover or preview by name, else the first in
natural order) and reads only that member, within size budgets: solid 7z and rar archives
must decompress everything before a member, so a member too deep inside one is given up.
"""

import logging
import re
import zipfile
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

import py7zr
import py7zr.io
import rarfile

from tagalot.themes.api import KIND_EXTENSIONS, Kind

logger = logging.getLogger(__name__)

MAX_MEMBER_BYTES = 64 * 1024 * 1024
"""Images larger than this (uncompressed) are skipped."""

SOLID_READ_BUDGET = 256 * 1024 * 1024
"""In a solid archive, give up when more than this must be decompressed to reach the image."""

PREFERRED_WORDS = ("cover", "preview", "thumb")
"""Member names containing these (in this order of preference) are chosen first."""

IMAGE_EXTENSIONS = KIND_EXTENSIONS[Kind.IMAGE]

ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
SEVENZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"
RAR_MAGIC = b"Rar!\x1a\x07"


class ArchiveError(Exception):
    """The file isn't an archive this reader understands, or a member can't be read."""


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    """The member's path inside the archive, with ``/`` separators."""
    size: int
    """Uncompressed size in bytes."""
    is_dir: bool = False
    encrypted: bool = False


class ArchiveReader(ABC):
    """One open archive. Use it as a context manager."""

    solid: bool = False
    """Whether reading a member means decompressing the ones before it."""

    @abstractmethod
    def members(self) -> list[ArchiveMember]:
        """Every member, in archive order."""

    @abstractmethod
    def read(self, member: ArchiveMember, limit: int) -> bytes:
        """The member's bytes; :class:`ArchiveError` if it has more than ``limit``."""

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "ArchiveReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_archive(path: str) -> ArchiveReader:
    """Open a zip, 7z, or rar archive, recognized by its first bytes."""
    with open(path, "rb") as file:
        head = file.read(8)
    if head.startswith(ZIP_MAGIC):
        return _ZipReader(path)
    if head.startswith(SEVENZIP_MAGIC):
        return _SevenZipReader(path)
    if head.startswith(RAR_MAGIC):
        return _RarReader(path)
    raise ArchiveError("not a zip, 7z, or rar archive")


# --- choosing ---


def is_candidate_image(member: ArchiveMember) -> bool:
    """An image file, not a folder, a dotfile, or macOS metadata (``__MACOSX/``)."""
    if member.is_dir:
        return False
    parts = PurePosixPath(member.name).parts
    if not parts or any(p.startswith(".") or p == "__MACOSX" for p in parts):
        return False
    return PurePosixPath(parts[-1]).suffix.lower() in IMAGE_EXTENSIONS


def natural_key(text: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key putting ``page2`` before ``page10``; case-insensitive."""
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", text.lower())
        if part
    )


def image_members(members: Sequence[ArchiveMember]) -> list[ArchiveMember]:
    """The archive's images, best first: names with a preferred word, then natural order."""

    def rank(member: ArchiveMember) -> tuple[int, tuple[tuple[int, int, str], ...]]:
        name = PurePosixPath(member.name).name.lower()
        word = next((i for i, w in enumerate(PREFERRED_WORDS) if w in name), len(PREFERRED_WORDS))
        return word, natural_key(member.name)

    return sorted(filter(is_candidate_image, members), key=rank)


def archive_image_bytes(path: str) -> bytes | None:
    """The bytes of the best image inside the archive at ``path``, or ``None`` when it has
    none within the budgets. Raises :class:`ArchiveError` (or ``OSError``) when the archive
    can't be read."""
    with open_archive(path) as archive:
        members = archive.members()
        position = {m.name: i for i, m in enumerate(members)}
        for member in image_members(members):
            if member.encrypted or member.size > MAX_MEMBER_BYTES:
                continue
            if archive.solid:
                before = sum(m.size for m in members[: position[member.name]])
                if before > SOLID_READ_BUDGET:
                    logger.info("Skipping %s: its image is too deep in a solid archive", path)
                    return None
            try:
                return archive.read(member, MAX_MEMBER_BYTES)
            except NoRarTool:
                return None  # logged once; not a problem with this file
    return None


# --- formats ---


class _ZipReader(ArchiveReader):
    def __init__(self, path: str) -> None:
        try:
            self._zip = zipfile.ZipFile(path)
        except zipfile.BadZipFile as e:
            raise ArchiveError(str(e)) from e

    def members(self) -> list[ArchiveMember]:
        return [
            ArchiveMember(info.filename, info.file_size, info.is_dir(), bool(info.flag_bits & 1))
            for info in self._zip.infolist()
        ]

    def read(self, member: ArchiveMember, limit: int) -> bytes:
        try:
            with self._zip.open(member.name) as stream:
                data = stream.read(limit + 1)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as e:
            raise ArchiveError(f"{member.name}: {e}") from e
        if len(data) > limit:
            raise ArchiveError(f"{member.name} is larger than {limit} bytes")
        return data

    def close(self) -> None:
        self._zip.close()


class _SevenZipReader(ArchiveReader):
    def __init__(self, path: str) -> None:
        try:
            self._archive = py7zr.SevenZipFile(path)
            self.solid = bool(self._archive.archiveinfo().solid)
            self._encrypted = self._archive.needs_password()
        except py7zr.exceptions.ArchiveError as e:
            raise ArchiveError(str(e)) from e

    def members(self) -> list[ArchiveMember]:
        return [
            ArchiveMember(info.filename, info.uncompressed, info.is_directory, self._encrypted)
            for info in self._archive.list()
        ]

    def read(self, member: ArchiveMember, limit: int) -> bytes:
        factory = py7zr.io.BytesIOFactory(limit)
        try:
            self._archive.reset()
            self._archive.extract(targets=[member.name], factory=factory)
            stream = factory.get(member.name)  # type: ignore[no-untyped-call]
        except (py7zr.exceptions.ArchiveError, py7zr.io.BufferOverflow, KeyError) as e:
            raise ArchiveError(f"{member.name}: {e}") from e
        stream.seek(0)
        return bytes(stream.read())

    def close(self) -> None:
        self._archive.close()


class _RarReader(ArchiveReader):
    _warned_no_tool = False

    def __init__(self, path: str) -> None:
        try:
            self._rar = rarfile.RarFile(path)
            self.solid = bool(self._rar.is_solid())
        except rarfile.Error as e:
            raise ArchiveError(str(e)) from e

    def members(self) -> list[ArchiveMember]:
        return [
            ArchiveMember(
                info.filename or "", info.file_size or 0, info.is_dir(), info.needs_password()
            )
            for info in self._rar.infolist()
        ]

    def read(self, member: ArchiveMember, limit: int) -> bytes:
        try:
            with self._rar.open(member.name) as stream:
                data = stream.read(limit + 1)
        except rarfile.RarCannotExec as e:
            if not _RarReader._warned_no_tool:
                _RarReader._warned_no_tool = True
                logger.warning("Rar thumbnails need unrar, 7-Zip, or bsdtar installed: %s", e)
            raise NoRarTool(str(e)) from e
        except rarfile.Error as e:
            raise ArchiveError(f"{member.name}: {e}") from e
        if len(data) > limit:
            raise ArchiveError(f"{member.name} is larger than {limit} bytes")
        return bytes(data)

    def close(self) -> None:
        self._rar.close()


class NoRarTool(ArchiveError):
    """No external tool for reading compressed rar members is installed."""
