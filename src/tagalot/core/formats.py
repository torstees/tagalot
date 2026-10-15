"""How values read for people: file sizes, durations, ratings, and web addresses
(DESIGN.md §9 "Fields").

A theme field can declare ``display="bytes"`` or ``display="duration"``; lists, cards,
detail pages, and filter chips show it this way, while sorting, filtering, and editing keep
using the stored number. ``display="url"`` on a text field makes an item's page show a web
address as a link (:func:`is_web_address`); elsewhere it reads as the address. A rating
(``field(..., rating=5)``, display ``"stars/5"`` or ``"stars/10"``) reads as five stars in
half-star steps (:func:`format_stars`).
"""

import re
from typing import Any


def format_bytes(size: float) -> str:
    """A size for people: ``"980 KB"``, ``"12.4 MB"`` (1 KB = 1024 bytes)."""
    value = float(size)
    unit = "bytes"
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            break
        value /= 1024
    if unit == "bytes":
        return f"{int(size):,} bytes" if int(size) != 1 else "1 byte"
    return f"{value:,.0f} {unit}" if abs(value) >= 100 else f"{value:,.1f} {unit}"


def format_duration(seconds: float) -> str:
    """A length of time: ``"3:25"``, ``"1:02:03"`` (rounded to the second)."""
    total = round(seconds)
    sign = "-" if total < 0 else ""
    hours, rest = divmod(abs(total), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{sign}{hours}:{minutes:02d}:{secs:02d}"
    return f"{sign}{minutes}:{secs:02d}"


FORMATTERS = {"bytes": format_bytes, "duration": format_duration}
"""The display formats a field may declare, by name."""

STARS = 5
"""A rating shows as this many stars, whatever it is out of."""
FULL, HALF, EMPTY = "\u2605", "\u00bd", "\u2606"  # ★ ½ ☆


def rating_scale(display: str | None) -> int | None:
    """What a rating field is out of (5 or 10), from its display (``"stars/5"``); ``None``
    for a field that isn't a rating."""
    if not display or not display.startswith("stars/"):
        return None
    scale = display.removeprefix("stars/")
    return int(scale) if scale.isdigit() and int(scale) > 0 else None


def half_stars(value: float, scale: int) -> int:
    """A rating as a number of half stars, 0 to 10: 3.5 out of 5 is 7, and so is 7 out of
    10 (each point a half star). Values between steps round to the nearest."""
    halves = round(float(value) * STARS * 2 / scale)
    return max(0, min(STARS * 2, halves))


def rating_of(halves: int, scale: int) -> float:
    """The rating a number of half stars stands for, out of ``scale`` (7 is 3.5 out of 5,
    7.0 out of 10)."""
    return halves * scale / (STARS * 2)


def format_stars(value: float, scale: int) -> str:
    """A rating as five stars: ``"★★★½☆"`` for 3.5 out of 5 (a half star reads ``½``)."""
    halves = half_stars(value, scale)
    full, half = divmod(halves, 2)
    return FULL * full + HALF * half + EMPTY * (STARS - full - half)


def format_rating_number(value: float, scale: int) -> str:
    """A rating in words, for tooltips and filters: ``"3.5 out of 5"``."""
    number = float(value)
    shown = f"{number:g}" if number == round(number, 1) else f"{number:.1f}"
    return f"{shown} out of {scale}"


WEB_ADDRESS = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


def is_web_address(value: Any) -> bool:
    """Whether ``value`` is an ``http`` or ``https`` address, the only ones a ``"url"``
    field opens (anything else shows as plain text)."""
    return isinstance(value, str) and WEB_ADDRESS.match(value.strip()) is not None


def format_value(value: Any, display: str | None) -> str | None:
    """``value`` in the field's display format, or ``None`` when the format doesn't apply
    (no format, not a number, or empty)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    scale = rating_scale(display)
    if scale is not None:
        return format_stars(value, scale)
    formatter = FORMATTERS.get(display or "")
    return formatter(value) if formatter is not None else None
