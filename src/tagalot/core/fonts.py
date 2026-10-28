"""A keep's own fonts (#565, DESIGN.md §12 *Fonts*): font files in the keep folder.

``[appearance.fonts]`` names a font per place (``FONT_PLACES``): an installed family, which
only the UI can look for, or a TTF or OTF file copied into the keep folder's ``fonts/``, so
the keep looks the same from another computer or a share. A file is copied once and never
replaced; another with the same name gets a number. The files are read here, in a worker,
and the UI registers them with Qt.
"""

import hashlib
import logging
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

KEEP_FONTS = "fonts"
"""The keep folder's folder for font files the user chose."""
FONT_EXTENSIONS = (".ttf", ".otf")
MAX_FONT = 32 * 1024 * 1024
"""The largest font file taken, in bytes."""


class FontFileError(ValueError):
    """A file that can't be a keep's font; the message is for the user."""


def is_font_file(value: str) -> bool:
    """Whether a ``[appearance.fonts]`` value names a file in the keep folder rather than
    an installed family."""
    return value.startswith(f"{KEEP_FONTS}/") and value.lower().endswith(FONT_EXTENSIONS)


def font_path(keep_dir: Path, value: str) -> Path | None:
    """The file a value names, inside the keep folder's ``fonts/``; None for a family, or a
    path that would leave that folder."""
    if not is_font_file(value):
        return None
    relative = PurePosixPath(value)
    if ".." in relative.parts or relative.is_absolute():
        return None
    return keep_dir.joinpath(*relative.parts)


def read_font_files(keep_dir: Path, fonts: Mapping[str, str]) -> dict[str, bytes | None]:
    """The font files a keep's fonts name, by value: their bytes, or None when the file is
    gone or can't be read (the place falls back to the system's). Does I/O: call it from a
    worker."""
    found: dict[str, bytes | None] = {}
    for value in fonts.values():
        path = font_path(keep_dir, value)
        if path is None or value in found:
            continue
        try:
            data = path.read_bytes()
        except OSError as e:
            logger.warning("Can't read the keep's font %s: %s", path, e)
            found[value] = None
            continue
        found[value] = data if len(data) <= MAX_FONT else None
    return found


def copy_font_file(keep_dir: Path, source: Path) -> str:
    """Copy a font file the user chose into the keep folder's ``fonts/`` and return its
    path there, as stored (``fonts/Literata.ttf``). Raises :class:`FontFileError`. Does
    I/O: call it from a worker."""
    suffix = source.suffix.lower()
    if suffix not in FONT_EXTENSIONS:
        raise FontFileError("Choose a TrueType (.ttf) or OpenType (.otf) font file.")
    try:
        data = source.read_bytes()
    except OSError as e:
        raise FontFileError(f"Couldn't read {source.name}: {e.strerror or e}") from None
    if len(data) > MAX_FONT:
        raise FontFileError(f"{source.name} is too big for a font (32 MB at most).")
    folder = keep_dir / KEEP_FONTS
    folder.mkdir(exist_ok=True)
    stem = source.stem.strip() or "font"
    digest = hashlib.blake2b(data).digest()
    for n in range(1, 1000):
        name = f"{stem}{suffix}" if n == 1 else f"{stem} {n}{suffix}"
        target = folder / name
        if target.is_file() and hashlib.blake2b(target.read_bytes()).digest() == digest:
            return f"{KEEP_FONTS}/{name}"  # already copied
        if not target.exists():
            partial = target.with_name(target.name + ".part")
            partial.write_bytes(data)
            partial.replace(target)
            return f"{KEEP_FONTS}/{name}"
    raise FontFileError(f"Too many fonts named {source.name} in the keep.")
