"""How numbers read for people: file sizes and durations (DESIGN.md §9 "Fields").

A theme field can declare ``display="bytes"`` or ``display="duration"``; lists, cards,
detail pages, and filter chips show it this way, while sorting, filtering, and editing keep
using the stored number.
"""

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


def format_value(value: Any, display: str | None) -> str | None:
    """``value`` in the field's display format, or ``None`` when the format doesn't apply
    (no format, not a number, or empty)."""
    formatter = FORMATTERS.get(display or "")
    if formatter is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return formatter(value)
