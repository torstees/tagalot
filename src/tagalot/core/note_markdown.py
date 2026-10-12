"""Showing a note's body on its item's page (DESIGN.md §9 *Notes*, §12, #380).

The page renders the body as Markdown. Its pictures and links are relative to the note's
folder: :func:`read_pictures` reads the pictures (in a worker, never on the GUI thread; the
page shows only what was read), and :func:`resolve_link` turns a link into a path on this
computer. Obsidian's ``![[picture.png]]`` embeds and ``[[Other note]]`` links are written
as plain Markdown first (:func:`renderable`), since editors that write notes use them.
A picture or link that isn't beside the note is looked for in its assets folders
(``core/assets.py``, #418): :func:`read_pictures` and :func:`find_links` check, in the
worker, so the page knows where each one is. Nothing here writes anything.
"""

import logging
import os
import re
from collections.abc import Sequence
from types import ModuleType
from urllib.parse import unquote

from tagalot.core.assets import asset_ref

logger = logging.getLogger(__name__)

MAX_PICTURES = 12
"""Pictures read for one note; the rest show as their text."""
MAX_PICTURE_BYTES = 10 * 1024 * 1024
"""A larger picture isn't read."""
PICTURE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
MAX_LINKS = 50
"""Links looked for in the assets folders, for one note; the rest open beside it."""

_EMBED = re.compile(r"!\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
"""Obsidian's ``![[picture.png|300]]``."""
_WIKI_LINK = re.compile(r"(?<!!)\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
"""Obsidian's ``[[Other note]]`` and ``[[Other note|shown as]]``."""
_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>\n]+)>|([^)\s]+))(?:\s+[\"'][^)]*[\"'])?\s*\)")
"""``![alt](path "title")``, the path in ``<…>`` when it has spaces."""
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(\s*(?:<([^>\n]+)>|([^)\s]+))")
"""``[text](path)``, not a picture."""
_IMG_TAG = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
"""``https:``, ``mailto:``, ``data:``…; a Windows drive (``C:``) is one letter, so it
isn't taken for one (absolute paths aren't followed anyway)."""


def renderable(body: str) -> str:
    """``body`` with Obsidian's embeds as Markdown pictures and its wiki links as text."""

    def embed(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        if not target.lower().endswith(PICTURE_EXTENSIONS):
            return target  # an embedded note: its name
        return f"![{target}](<{target}>)"

    body = _EMBED.sub(embed, body)
    return _WIKI_LINK.sub(lambda m: (m.group(2) or m.group(1)).strip(), body)


def picture_refs(body: str) -> list[str]:
    """The pictures a (renderable) body shows, as written, in order, each once."""
    found: dict[str, None] = {}
    for match in _IMAGE.finditer(body):
        found.setdefault(match.group(1) or match.group(2), None)
    for match in _IMG_TAG.finditer(body):
        found.setdefault(match.group(1), None)
    return list(found)


def resolve_link(folder: str, ref: str, pathmod: ModuleType = os.path) -> str | None:
    """The path on this computer a relative link or picture in a note names, or ``None``
    for a web address, an anchor, or an absolute path. ``folder`` is the note's folder."""
    ref = ref.strip()
    if not ref or ref.startswith(("#", "/", "\\")) or _SCHEME.match(ref):
        return None
    ref = unquote(ref.split("#", 1)[0].split("?", 1)[0])
    if not ref or pathmod.isabs(ref):
        return None
    return str(pathmod.normpath(pathmod.join(folder, *ref.replace("\\", "/").split("/"))))


def find_file(folder: str, ref: str, assets: Sequence[str] = ()) -> str | None:
    """Where a relative link or picture is on this computer: beside the note (in
    ``folder``) if it is there, else in the first of the ``assets`` folders (this computer's
    paths) that has it, else beside the note anyway (``None`` as for :func:`resolve_link`).
    Checks the disk: call it from a worker."""
    path = resolve_link(folder, ref)
    if path is None or not assets or os.path.exists(path):
        return path
    clean = unquote(ref.strip().split("#", 1)[0].split("?", 1)[0])
    name = asset_ref(clean)
    if name is not None:
        for assets_folder in assets:
            found = os.path.normpath(os.path.join(assets_folder, *name.split("/")))
            if os.path.exists(found):
                return found
    return path


def link_refs(body: str) -> list[str]:
    """The links (not pictures) a renderable body has, as written, in order, each once."""
    found: dict[str, None] = {}
    for match in _LINK.finditer(body):
        found.setdefault(match.group(1) or match.group(2), None)
    return list(found)


def find_links(body: str, folder: str, assets: Sequence[str]) -> dict[str, str]:
    """Where the note's relative links are, when not beside it: ``{path beside the note:
    path in an assets folder}`` (at most :data:`MAX_LINKS` looked for). Checks the disk."""
    found: dict[str, str] = {}
    if not assets:
        return found
    for ref in link_refs(body)[:MAX_LINKS]:
        beside = resolve_link(folder, ref)
        path = find_file(folder, ref, assets)
        if beside is not None and path is not None and path != beside:
            found[beside] = path
    return found


def read_pictures(body: str, folder: str, assets: Sequence[str] = ()) -> dict[str, bytes]:
    """The pictures a renderable body shows, read from the note's folder (else from its
    ``assets`` folders, this computer's paths), by the name written in the note: at most
    :data:`MAX_PICTURES`, each at most :data:`MAX_PICTURE_BYTES`. A picture that can't be
    read is left out (and logged)."""
    pictures: dict[str, bytes] = {}
    for ref in picture_refs(body):
        if len(pictures) >= MAX_PICTURES:
            break
        path = find_file(folder, ref, assets)
        if path is None or not path.lower().endswith(PICTURE_EXTENSIONS):
            continue
        try:
            if os.path.getsize(path) > MAX_PICTURE_BYTES:
                continue
            with open(path, "rb") as f:
                pictures[ref] = f.read()
        except OSError as e:
            logger.info("Note picture %s not read: %s", path, e)
    return pictures
