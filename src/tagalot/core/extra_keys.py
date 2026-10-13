"""Front-matter keys as Extra fields: what notes (§9 *Notes*) and Markdown files' own front
matter (``ctx.extra``, #410) share.

A key becomes an Extra field named as written, its value as text (:func:`as_text`), unless
it is Tagalot's own (``tagalot-…``), one the core reads itself (``title``, ``tags``,
``keywords``, ``aliases``), or ignored (the built-in, the user's, and the keep's lists,
``core/note_keys.py``).
"""

import datetime
from collections.abc import Collection, Mapping
from typing import Any

DEFAULT_IGNORED_KEYS = frozenset({"cssclasses", "cssclass", "publish", "permalink", "position"})
"""Keys editors add for themselves, never Extra fields (§9 *Notes*: built-in defaults)."""
RESERVED_PREFIX = "tagalot-"
"""Keys Tagalot reads itself (``tagalot-type``): never Extra fields."""
EXTRA = "extra:"
"""Provenance rows for an Extra field are named ``extra:<name>``."""
READ_KEYS = frozenset({"title", "tags", "keywords", "aliases"})
"""Keys a Markdown file's front matter gives the item otherwise, never as Extra fields."""


def as_text(value: Any) -> str | None:
    """A front-matter value as an Extra field's text: lists joined with ``, ``, dates as
    ``YYYY-MM-DD`` (and times as ``HH:MM``), yes/no as ``true``/``false``; ``None`` for a
    table of keys, which isn't kept."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, int | float | str):
        return " ".join(str(value).split()) or None
    if isinstance(value, list | tuple):
        parts = [t for item in value if (t := as_text(item)) is not None]
        return ", ".join(parts) or None
    return None


def skipped(key: str, ignored: Collection[str] = ()) -> bool:
    """Whether ``key`` (stripped) never becomes an Extra field; ``ignored`` is case-folded."""
    folded = key.casefold()
    return (
        not key
        or folded.startswith(RESERVED_PREFIX)
        or folded in READ_KEYS
        or folded in DEFAULT_IGNORED_KEYS
        or folded in ignored
    )


def extra_values(values: Mapping[Any, Any], ignored: Collection[str] = ()) -> dict[str, str]:
    """``values`` as Extra fields: name (as written) -> text, without the keys
    :func:`skipped` leaves out, empty values, and tables of keys."""
    found: dict[str, str] = {}
    for raw_key, value in values.items():
        key = str(raw_key).strip()
        if skipped(key, ignored):
            continue
        text = as_text(value)
        if text is not None:
            found[key] = text
    return found
