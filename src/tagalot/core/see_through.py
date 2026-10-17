"""See-through folders (#459, DESIGN.md §6 *See-through folders*): folders named in the keep's
``[folders] see_through`` (``! Posters``) are never items, and the files in them count as their
parent folder's.

A path is see-through when one of its folders has such a name, case aside. A file in one (at
any depth) counts as in the nearest folder above it that isn't one (:func:`counts_in`); such a
folder, and every folder inside it, is hidden (:func:`hidden_dir`). Paths are root-relative
POSIX, as stored.
"""

import posixpath
from collections.abc import Iterable


def names_of(names: Iterable[str]) -> frozenset[str]:
    """The names as compared: case-folded."""
    return frozenset(n.casefold() for n in names if n)


def counts_in(relpath: str, names: frozenset[str]) -> str | None:
    """The folder a file counts as in when one of its folders is see-through: the folder
    above the first see-through one (``""``, the top of the root); ``None`` when none is."""
    if not names:
        return None
    parts = relpath.split("/")[:-1]
    for i, part in enumerate(parts):
        if part.casefold() in names:
            return "/".join(parts[:i])
    return None


def hidden_dir(relpath: str, names: frozenset[str]) -> bool:
    """A folder that is see-through, or inside one: never an item, nor a folder the theme
    sees."""
    return bool(names) and any(part.casefold() in names for part in relpath.split("/"))


def folder_of(relpath: str, names: frozenset[str]) -> str:
    """The folder a file counts as in: its own, or the one :func:`counts_in` gives."""
    found = counts_in(relpath, names)
    return posixpath.dirname(relpath) if found is None else found


def logical(relpath: str, names: frozenset[str]) -> str:
    """The path a file counts as having: in :func:`folder_of`, by its own name."""
    found = counts_in(relpath, names)
    if found is None:
        return relpath
    name = relpath.rpartition("/")[2]
    return f"{found}/{name}" if found else name
