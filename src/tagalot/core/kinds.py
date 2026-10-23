"""Kinds of items, for hiding some of a list (DESIGN.md §8 *Kinds*, #512).

An item's **kinds** are the file types of the files in its type's primary role (a book's
EPUB and PDF), or, when it has no such file, its type (a folder, an actor). A file type is
its extension, lowercase with the dot, with well-known aliases as one (``.jpeg`` is
``.jpg``); a file without an extension is ``"."``. A type is its type id, which never
starts with a dot, so the two never clash.
"""

from sqlalchemy import ColumnElement, case

from tagalot.core.models import Resource

FILES_KEY = "@files"
"""The key, in a search's ``fields``, of the condition that picks an item's own files: the
``entity_resource`` rows (joined to ``entity``) in its type's primary role
(:func:`~tagalot.core.search_fields.primary_files`)."""

NO_EXTENSION = "."
"""The kind of a file without an extension."""

ALIASES = {
    ".jpeg": ".jpg",
    ".jpe": ".jpg",
    ".tiff": ".tif",
    ".htm": ".html",
    ".mpeg": ".mpg",
    ".yml": ".yaml",
}
"""Extensions that are the same file type, as the one kind they count as."""


def file_kind(ext: str) -> str:
    """The kind of a file with extension ``ext`` (lowercase, with the dot, or empty)."""
    if not ext:
        return NO_EXTENSION
    return ALIASES.get(ext, ext)


def is_file_kind(kind: str) -> bool:
    """Whether ``kind`` is a file type rather than an item type."""
    return kind.startswith(".")


def kind_column() -> ColumnElement[str]:
    """:func:`file_kind` of ``resource.ext``, in SQL."""
    ext = Resource.ext
    whens: list[tuple[ColumnElement[bool], str]] = [(ext == "", NO_EXTENSION)]
    whens.extend((ext == alias, kind) for alias, kind in ALIASES.items())
    return case(*whens, else_=ext)
