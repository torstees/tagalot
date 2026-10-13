"""Assets folders: where a Markdown file's relative pictures and links are looked for when
they aren't beside it (DESIGN.md §9 *Markdown*, #418).

Editors often keep a Markdown file's pictures in an attachments folder rather than next to
it. A file names a picture as ``covers/dune.jpg``; Tagalot looks for it beside the file
first (what is written always wins), then under each assets folder in turn:

1. the item's own (its ``assets`` field, from front matter or edited; relative to the file's
   folder, or to the top of its watched folder with a leading ``/``);
2. the watched folder's default (``[[roots]] assets`` in ``keep.toml``);
3. the keep's default (``[markdown] assets``).

A default is relative to the top of the watched folder, or to the file's own folder when it
starts with ``./``. Every folder stays inside the file's watched folder: one that would
leave it is dropped. Nothing here touches a disk.
"""

import posixpath
from collections.abc import Iterable

from tagalot.themes.readers import no_path

ASSETS = "assets"
"""The front-matter key, and the field or Extra field, naming an item's assets folder."""


def _inside(path: str) -> str | None:
    """``path`` normalized inside its root, or ``None`` if it leaves it."""
    path = posixpath.normpath(path) if path else "."
    if path == ".." or path.startswith("../"):
        return None
    return "" if path == "." else path


def _clean(value: str | None) -> str:
    return "" if no_path(value) else (value or "").strip().replace("\\", "/")


def clean_setting(value: str) -> str:
    """An assets folder as Keep configuration saves it: trimmed, with ``/`` separators.
    Raises ``ValueError`` for one that names a drive or a web address, or climbs out of the
    watched folder: it must be a folder inside it."""
    value = _clean(value)
    if not value:
        return ""
    if ":" in value.split("/", 1)[0] or value.startswith("//"):
        raise ValueError(f"An assets folder is a folder inside the watched folder, not {value}.")
    if _inside(value.removeprefix("./").lstrip("/")) is None:
        raise ValueError(f"An assets folder can't be outside the watched folder: {value}.")
    return value


def assets_folders(relpath: str, own: str | None, defaults: Iterable[str | None]) -> list[str]:
    """The root-relative folders to look in, in order, for a file ``relpath`` (root-relative
    POSIX) names: its item's own assets folder ``own`` (relative to the file's folder, or to
    the root with a leading ``/``), then each default (relative to the root, or to the file's
    folder with a leading ``./``). Folders leaving the root, and repeats, are left out; so is
    the file's own folder, which is always looked in first anyway."""
    here = posixpath.dirname(relpath)
    found: list[str] = []
    candidates: list[str | None] = []
    own = _clean(own)
    if own:
        candidates.append(own[1:] if own.startswith("/") else posixpath.join(here, own))
    for value in defaults:
        value = _clean(value)
        if not value:
            continue
        if value == "." or value.startswith("./"):
            candidates.append(posixpath.join(here, value))
        else:
            candidates.append(value.lstrip("/"))
    for candidate in candidates:
        folder = _inside(candidate) if candidate is not None else None
        if folder is not None and folder != here and folder not in found:
            found.append(folder)
    return found


def asset_ref(ref: str) -> str | None:
    """A name as written in a file (``covers/dune.jpg``), POSIX, if it can be looked for
    under an assets folder: relative, and not climbing out with ``..``. ``None`` otherwise
    (a ``/``-rooted path, a web address, ``../x.jpg``)."""
    ref = _clean(ref)
    if not ref or ref.startswith("/") or ":" in ref.split("/", 1)[0]:
        return None
    path = _inside(ref)
    return path or None


def in_assets(folders: Iterable[str], ref: str) -> list[str]:
    """Where ``ref`` would be under each of ``folders``, root-relative, in order."""
    name = asset_ref(ref)
    if name is None:
        return []
    return [posixpath.join(folder, name) if folder else name for folder in folders]
