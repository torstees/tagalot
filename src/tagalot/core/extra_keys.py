"""Front-matter keys as Extra fields: what notes (§9 *Notes*) and Markdown files' own front
matter (``ctx.extra``, #410) share.

A key becomes an Extra field named as written, its value as text (:func:`as_text`), unless
it is Tagalot's own (``tagalot-…``), one the core reads itself (``title``, ``tags``,
``keywords``, ``aliases``), or ignored (the built-in, the user's, and the keep's lists,
``core/note_keys.py``).
"""

import datetime
import re
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from sqlalchemy import Connection, select

from tagalot.core.models import NoteKeyAlias
from tagalot.themes.api import FieldInfo

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


PICTURE_KEYS = ("cover", "image")
"""A note's keys naming its item's picture, the first given winning: never fields."""
NOT_ALIASED = READ_KEYS | set(PICTURE_KEYS)
"""Keys Tagalot reads itself, which an alias of the user's can't make a field (#411)."""


def loose_key(text: str) -> str:
    """A key or field name as notes match them: case, spaces, ``_`` and ``-`` aside."""
    return re.sub(r"[\s_\-]+", "", text).casefold()


def field_keys(fields: Iterable[FieldInfo]) -> dict[str, FieldInfo]:
    """Each field by its name and its label, as :func:`loose_key` makes them."""
    by_key: dict[str, FieldInfo] = {}
    for info in fields:
        by_key.setdefault(loose_key(info.name), info)
        by_key.setdefault(loose_key(info.spec.label), info)
    return by_key


def aliased_fields(fields: Iterable[FieldInfo], aliases: Mapping[str, str]) -> dict[str, FieldInfo]:
    """The user's key aliases (#411; key -> a field's name or label) that name one of
    ``fields``, by the aliased key's :func:`loose_key`. A key that is itself a field's name or
    label isn't aliased (the field is read under it), nor is one Tagalot reads itself."""
    by_key = field_keys(fields)
    found: dict[str, FieldInfo] = {}
    for key, target in aliases.items():
        loose = loose_key(key)
        folded = key.strip().casefold()
        if not loose or loose in by_key or folded in NOT_ALIASED:
            continue
        if folded.startswith(RESERVED_PREFIX):
            continue
        if (info := by_key.get(loose_key(target))) is not None:
            found[loose] = info
    return found


def applied_aliases(conn: Connection) -> dict[str, str]:
    """The user's key aliases the keep's notes and Markdown files are read with (#411):
    case-folded key -> a field's name or label, as the last scan remembered them
    (``core.note_keys.sync_aliases``)."""
    return {k: v for k, v in conn.execute(select(NoteKeyAlias.key, NoteKeyAlias.field))}


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


def convert(value: Any, kind: type) -> Any:
    """``value`` as a field of type ``kind``; ``ValueError`` when it isn't one."""
    if kind is str:
        text = as_text(value)
        if text is None:
            raise ValueError("isn't text")
        return text
    if kind is bool:
        if isinstance(value, bool):
            return value
        words = {"true": True, "yes": True, "false": False, "no": False}
        if isinstance(value, str) and value.strip().casefold() in words:
            return words[value.strip().casefold()]
        raise ValueError("isn't yes or no")
    if isinstance(value, bool):
        raise ValueError(f"isn't a {_names[kind]}")
    if kind is int:
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"\s*-?[\d,]+\s*", value):
            return int(value.replace(",", ""))
        raise ValueError("isn't a whole number")
    if kind is float:
        if isinstance(value, int | float):
            return float(value)
        try:
            return float(str(value).replace(",", ""))
        except ValueError:
            raise ValueError("isn't a number") from None
    if kind is datetime.datetime:
        when = value
        if isinstance(value, str):
            try:
                when = datetime.datetime.fromisoformat(value.strip())
            except ValueError:
                raise ValueError("isn't a date and time") from None
        elif isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
            when = datetime.datetime(value.year, value.month, value.day)
        if not isinstance(when, datetime.datetime):
            raise ValueError("isn't a date and time")
        return (when if when.tzinfo else when.astimezone()).astimezone(datetime.UTC)
    if kind is datetime.date:
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        try:
            return datetime.date.fromisoformat(str(value).strip())
        except ValueError:
            raise ValueError("isn't a date (YYYY-MM-DD)") from None
    raise ValueError(f"can't be read as {kind.__name__}")


_names = {int: "whole number", float: "number", datetime.date: "date", datetime.datetime: "date"}
