"""Windows shortcuts (``.lnk``) read as links (DESIGN.md §13 *Linked places*, #602).

A small reader of the Shell Link format ([MS-SHLLINK]): the header's flags and the
target's attributes, the LinkInfo's local path and network path, and the relative path
from the string data. No dependency, and it runs on any OS (a share's shortcuts seen
from Linux or macOS too).

:func:`shortcut_target` resolves a shortcut to a path on this computer: its relative path
(from the shortcut's folder), else its local path, else its network path, the first that
is there. :func:`on_disk` reads a path through the shortcuts in it, for paths inside a
folder shortcut (``Jane/Book.lnk/chapter 1.pdf``), which exist only through it.
"""

import logging
import os
import struct
from dataclasses import dataclass

logger = logging.getLogger(__name__)

EXT = ".lnk"
MAX_SIZE = 1 << 20
"""A shortcut is a few hundred bytes; bigger ``.lnk`` files aren't read as shortcuts."""

_HEADER_SIZE = 0x4C
_CLSID = bytes.fromhex("0114020000000000c000000000000046")
_HAS_ID_LIST = 0x1
_HAS_LINK_INFO = 0x2
_HAS_NAME = 0x4
_HAS_RELATIVE_PATH = 0x8
_IS_UNICODE = 0x80
_DIRECTORY = 0x10
_VOLUME_AND_LOCAL_PATH = 0x1
_NETWORK_AND_SUFFIX = 0x2


@dataclass(frozen=True)
class Shortcut:
    """What a shortcut says about its target."""

    relative: str | None = None
    """Its path from the shortcut's folder (``..\\Books\\x.pdf``)."""
    local: str | None = None
    """Its path on the computer that made the shortcut (``D:\\Books\\x.pdf``)."""
    network: str | None = None
    """Its network path (``\\\\server\\share\\Books\\x.pdf``)."""
    is_dir: bool = False
    """The target was a folder when the shortcut was made."""


def is_shortcut_name(name: str) -> bool:
    return name.lower().endswith(EXT)


def read_shortcut(path: str) -> Shortcut | None:
    """The shortcut at ``path``, or ``None`` if it isn't one (or can't be read)."""
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_SIZE + 1)
    except OSError as e:
        logger.info("Cannot read shortcut %s: %s", path, e)
        return None
    if len(data) > MAX_SIZE:
        return None
    try:
        return parse_shortcut(data)
    except (struct.error, ValueError, IndexError) as e:
        logger.info("Not a shortcut %s: %s", path, e)
        return None


def parse_shortcut(data: bytes) -> Shortcut | None:
    """A shortcut's target, from its bytes; ``None`` if they aren't a shortcut."""
    if len(data) < _HEADER_SIZE or data[:4] != struct.pack("<I", _HEADER_SIZE):
        return None
    if data[4:20] != _CLSID:
        return None
    flags, attributes = struct.unpack_from("<II", data, 0x14)
    unicode = bool(flags & _IS_UNICODE)
    at = _HEADER_SIZE
    if flags & _HAS_ID_LIST:
        (size,) = struct.unpack_from("<H", data, at)
        at += 2 + size
    local = network = None
    if flags & _HAS_LINK_INFO:
        (size,) = struct.unpack_from("<I", data, at)
        local, network = _link_info(data[at : at + size])
        at += size
    relative = None
    for flag in (_HAS_NAME, _HAS_RELATIVE_PATH):
        if flags & flag:
            text, at = _string(data, at, unicode)
            if flag == _HAS_RELATIVE_PATH:
                relative = text or None
    return Shortcut(relative, local or None, network or None, bool(attributes & _DIRECTORY))


def _link_info(info: bytes) -> tuple[str | None, str | None]:
    """The LinkInfo structure's local path and network path."""
    header, flags, _volume, local_at, network_at, suffix_at = struct.unpack_from("<IIIIII", info, 4)
    local_u = suffix_u = 0
    if header >= 0x24:
        local_u, suffix_u = struct.unpack_from("<II", info, 28)
    suffix = _unicode_z(info, suffix_u) if suffix_u else _ansi_z(info, suffix_at)
    local = network = None
    if flags & _VOLUME_AND_LOCAL_PATH:
        base = _unicode_z(info, local_u) if local_u else _ansi_z(info, local_at)
        local = base + suffix
    if flags & _NETWORK_AND_SUFFIX:
        net = info[network_at:]
        (name_at,) = struct.unpack_from("<I", net, 8)
        name_u = struct.unpack_from("<I", net, 20)[0] if name_at > 0x14 else 0
        share = _unicode_z(net, name_u) if name_u else _ansi_z(net, name_at)
        network = share.rstrip("\\") + ("\\" + suffix if suffix else "")
    return local, network


def _ansi_z(data: bytes, at: int) -> str:
    end = data.index(b"\0", at)
    return data[at:end].decode("cp1252", errors="replace")


def _unicode_z(data: bytes, at: int) -> str:
    end = at
    while data[end : end + 2] != b"\0\0":
        end += 2
        if end >= len(data):
            raise ValueError("unterminated string")
    return data[at:end].decode("utf-16-le", errors="replace")


def _string(data: bytes, at: int, unicode: bool) -> tuple[str, int]:
    """A StringData entry: a count of characters, then the characters."""
    (count,) = struct.unpack_from("<H", data, at)
    at += 2
    size = count * 2 if unicode else count
    raw = data[at : at + size]
    if len(raw) != size:
        raise ValueError("string runs past the end")
    text = raw.decode("utf-16-le" if unicode else "cp1252", errors="replace")
    return text, at + size


def shortcut_target(path: str, shortcut: Shortcut | None = None) -> str | None:
    """Where the shortcut at ``path`` leads on this computer: the first of its relative
    path, local path, and network path that is there; ``None`` if none is (or it isn't a
    shortcut)."""
    found = shortcut if shortcut is not None else read_shortcut(path)
    if found is None:
        return None
    for candidate in candidates(path, found):
        if os.path.exists(candidate):
            return os.path.normpath(candidate)
    return None


def candidates(path: str, shortcut: Shortcut) -> list[str]:
    """The paths a shortcut may lead to here, in the order they are tried."""
    found: list[str] = []
    if shortcut.relative:
        relative = shortcut.relative.replace("\\", os.sep)
        found.append(os.path.join(os.path.dirname(path), relative))
    for absolute in (shortcut.local, shortcut.network):
        if absolute:
            found.append(absolute if os.sep == "\\" else absolute.replace("\\", os.sep))
    return found


def on_disk(path: str) -> str:
    """``path`` read through the folder shortcuts in it (and a shortcut at its end): the
    path that holds the file here. A path through no shortcut is itself."""
    if EXT not in path.lower():
        return path
    drive, rest = os.path.splitdrive(path)
    parts = [p for p in rest.replace("/", os.sep).split(os.sep)]
    current = drive + (os.sep if rest.startswith(("/", os.sep)) else "")
    for part in parts:
        if not part:
            continue
        current = os.path.join(current, part)
        if is_shortcut_name(part) and os.path.isfile(current):
            target = shortcut_target(current)
            if target is not None:
                current = target
    return current
