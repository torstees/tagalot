"""A keep's own fonts (#565, DESIGN.md §12 *Fonts*): font files in the keep folder.

``[appearance.fonts]`` names a font per place (``FONT_PLACES``): an installed family, which
only the UI can look for, or a TTF or OTF file copied into the keep folder's ``fonts/``, so
the keep looks the same from another computer or a share. A file is copied once and never
replaced; another with the same name gets a number. The files are read here, in a worker,
and the UI registers them with Qt.
"""

import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

KEEP_FONTS = "fonts"
SYSTEM_FONT = "system"
"""A place's value for the system's font, chosen over the theme's suggestion (#567)."""
GENERIC_FAMILIES = ("serif", "sans-serif", "monospace", "cursive", "fantasy")
"""Families that end a theme's chain: the system's font of that kind."""
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
    gone or can't be read (the place falls back to the system's). A file in a family's
    own folder (``fonts/Literata/…``, a Google Font, #566) brings the folder's other font
    files too, its bold and italic. Does I/O: call it from a worker."""
    found: dict[str, bytes | None] = {}
    for value in fonts.values():
        path = font_path(keep_dir, value)
        if path is None or value in found:
            continue
        found[value] = _read(path)
        if path.parent != keep_dir / KEEP_FONTS:
            for sibling in _family_files(path.parent):
                key = f"{KEEP_FONTS}/{path.parent.name}/{sibling.name}"
                if key not in found:
                    found[key] = _read(sibling)
    return found


def _read(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError as e:
        logger.warning("Can't read the keep's font %s: %s", path, e)
        return None
    return data if len(data) <= MAX_FONT else None


def _family_files(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.iterdir() if p.suffix.lower() in FONT_EXTENSIONS)
    except OSError:
        return []


def family_folder(name: str) -> str:
    """The folder of the keep's ``fonts/`` a family downloaded into the keep goes in (#566):
    its name, without characters a file name can't have."""
    return re.sub(r'[<>:"/\\|?*]', "", name).strip() or "font"


def is_theme_font_file(entry: str) -> bool:
    """Whether an entry of a theme's font chain is a file beside the theme's module."""
    return entry.lower().endswith(FONT_EXTENSIONS)


def theme_font_files(
    keep_dir: Path, fonts: Mapping[str, Sequence[str]], theme_dir: Path | None
) -> dict[str, bytes | None]:
    """The font files a theme's suggestions (#567) may show: the keep's downloaded
    families named in its chains (``fonts/<family>/…``), and files shipped beside the
    theme's module (``theme:<path>``). Does I/O: call it from a worker."""
    found: dict[str, bytes | None] = {}
    for chain in fonts.values():
        for entry in chain:
            if entry in GENERIC_FAMILIES:
                continue
            if is_theme_font_file(entry):
                relative = PurePosixPath(entry)
                if theme_dir is None or ".." in relative.parts or relative.is_absolute():
                    continue
                found[f"theme:{entry}"] = _read(theme_dir.joinpath(*relative.parts))
                continue
            folder = keep_dir / KEEP_FONTS / family_folder(entry)
            for path in _family_files(folder):
                found[f"{KEEP_FONTS}/{folder.name}/{path.name}"] = _read(path)
    return found


def theme_font_problems(theme: object) -> list[str]:
    """What's wrong with a theme's ``fonts`` and ``text_size`` (the loader refuses the
    theme)."""
    problems = []
    fonts = getattr(theme, "fonts", {})
    if not isinstance(fonts, Mapping) or not all(
        isinstance(place, str)
        and isinstance(chain, (list, tuple))
        and chain
        and all(isinstance(e, str) and e.strip() for e in chain)
        for place, chain in fonts.items()
    ):
        problems.append('fonts must map places ("titles", "notes", "text") to tuples of font names')
    size = getattr(theme, "text_size", None)
    if size is not None and (
        isinstance(size, bool) or not isinstance(size, int) or not 50 <= size <= 200
    ):
        problems.append("text_size must be a percent from 50 to 200")
    return problems


def theme_font_warnings(theme: object, theme_dir: Path | None) -> list[str]:
    """Places a theme's ``fonts`` names that Tagalot doesn't know, and font files it names
    that aren't beside it (``--check-theme`` warns; the theme still loads)."""
    from tagalot.core.keep import FONT_PLACES

    fonts = getattr(theme, "fonts", {})
    if not isinstance(fonts, Mapping):
        return []
    warnings = []
    for place, chain in fonts.items():
        if place not in FONT_PLACES:
            warnings.append(
                f"fonts: {place!r} isn't a place for a font (titles, notes, text); it's ignored"
            )
        for entry in chain if isinstance(chain, (list, tuple)) else ():
            if isinstance(entry, str) and is_theme_font_file(entry):
                path = theme_dir / entry if theme_dir is not None else None
                if path is None or not path.is_file():
                    warnings.append(f"fonts: {entry!r} isn't a file beside the theme")
    return warnings


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
