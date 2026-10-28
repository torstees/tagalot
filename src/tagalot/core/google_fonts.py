"""Google Fonts for a keep (#566, DESIGN.md §12 *Fonts*): the bundled list, and downloading a
family into the keep folder when its user asks.

The list (``resources/google_fonts.json``, made by ``scripts/fetch_google_fonts.py``) names
each family's category, its folder in the ``google/fonts`` repository on GitHub, the files to
fetch (its variable fonts, else its regular, bold, italic, and bold italic), and its license
file, at a pinned commit. A download fetches them from :data:`HOST` only, and only when the
user presses **Download**; nothing else in Tagalot goes there. The fonts' licenses (the SIL
Open Font License, Apache, the Ubuntu Font License) allow copying them into a keep; the
license file is copied beside them.

A family goes in its own folder of the keep's ``fonts/`` (``fonts/Literata/``), its files
named without the axes Google gives variable fonts (``Literata.ttf``,
``Literata-Italic.ttf``), and ``keep.toml`` names the first, so a keep opened elsewhere needs
no download. A file already there is kept, never replaced.
"""

import functools
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from urllib.parse import quote, urlsplit

from tagalot.core.fonts import KEEP_FONTS, MAX_FONT

logger = logging.getLogger(__name__)

HOST = "raw.githubusercontent.com"
"""Where Google Fonts are downloaded from: the ``google/fonts`` repository's files."""
CATEGORIES = ("serif", "sans-serif", "display", "handwriting", "monospace")

Fetch = Callable[[str], bytes]
Progress = Callable[[int, int], None]
"""Files fetched so far, of how many."""


class GoogleFontError(Exception):
    """A family that couldn't be downloaded; the message is for the user."""


@dataclass(frozen=True)
class GoogleFont:
    """A family in the bundled list."""

    name: str
    category: str
    """One of :data:`CATEGORIES`."""
    folder: str
    """Its folder in the repository (``ofl/literata``)."""
    files: tuple[str, ...]
    """The files fetched, the regular (or upright variable) font first."""
    license: str
    """Its license file in that folder (``OFL.txt``)."""

    @property
    def keep_folder(self) -> str:
        """Its folder in the keep's ``fonts/``: its name, without characters a file name
        can't have."""
        return re.sub(r'[<>:"/\\|?*]', "", self.name).strip() or "font"

    @property
    def value(self) -> str:
        """What ``keep.toml`` names it by: the first file in its folder."""
        return f"{KEEP_FONTS}/{self.keep_folder}/{local_name(self.files[0])}"


@dataclass(frozen=True)
class FontList:
    source: str
    """The repository's files at the pinned commit: ``https://raw.githubusercontent.com/…``."""
    families: tuple[GoogleFont, ...]


def local_name(name: str) -> str:
    """A file's name in the keep: without a variable font's axes (``Literata[opsz,wght].ttf``
    is ``Literata.ttf``)."""
    return re.sub(r"\[[^\]]*\]", "", name)


@functools.cache
def google_fonts() -> FontList:
    """The bundled list of Google Fonts, most used first."""
    text = resources.files("tagalot.resources").joinpath("google_fonts.json").read_text("utf-8")
    data = json.loads(text)
    families = tuple(
        GoogleFont(name, category, folder, tuple(files), license_file)
        for name, category, folder, files, license_file in data["families"]
    )
    return FontList(data["source"], families)


def downloaded(keep_dir: Path, fonts: tuple[GoogleFont, ...] | None = None) -> set[str]:
    """The names of the families already in the keep folder. Does I/O: call it from a
    worker."""
    folder = keep_dir / KEEP_FONTS
    try:
        present = {p.name for p in folder.iterdir() if p.is_dir()}
    except OSError:
        return set()
    found = set()
    for font in fonts if fonts is not None else google_fonts().families:
        if font.keep_folder in present and (keep_dir / font.value).is_file():
            found.add(font.name)
    return found


def fetch_url(url: str) -> bytes:
    """Fetch a file from :data:`HOST` (and nowhere else), up to :data:`~tagalot.core.fonts.
    MAX_FONT` bytes."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != HOST:
        raise GoogleFontError(f"Fonts are only downloaded from {HOST}.")
    request = urllib.request.Request(url, headers={"User-Agent": "Tagalot"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = bytes(response.read(MAX_FONT + 1))
    if len(data) > MAX_FONT:
        raise GoogleFontError("A font file is too big (32 MB at most).")
    return data


def download_font(
    keep_dir: Path,
    font: GoogleFont,
    *,
    fetch: Fetch = fetch_url,
    progress: Progress | None = None,
    source: str | None = None,
) -> str:
    """Download a family's files and license into the keep folder's ``fonts/<family>/``
    (files already there are kept) and return what ``keep.toml`` names it by. Raises
    :class:`GoogleFontError`. Does I/O and goes online: call it from a worker, and only
    when the user asked."""
    base = source or google_fonts().source
    folder = keep_dir / KEEP_FONTS / font.keep_folder
    names = [*font.files, font.license]
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise GoogleFontError(f"Couldn't make the folder {folder}: {e}") from e
    for done, name in enumerate(names):
        if progress is not None:
            progress(done, len(names))
        target = folder / local_name(name)
        if target.exists():
            continue
        url = f"{base}/{font.folder}/{quote(name)}"
        try:
            data = fetch(url)
        except urllib.error.HTTPError as e:
            raise GoogleFontError(f"{HOST} answered {e.code} for {name}.") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise GoogleFontError(f"Couldn't reach {HOST}: {reason}.") from e
        try:
            partial = target.with_name(target.name + ".part")
            partial.write_bytes(data)
            partial.replace(target)
        except OSError as e:
            raise GoogleFontError(f"Couldn't save {target.name}: {e}") from e
    if progress is not None:
        progress(len(names), len(names))
    logger.info("Downloaded the Google Font %s into %s", font.name, folder)
    return font.value
