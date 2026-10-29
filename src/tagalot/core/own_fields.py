"""Fields of the user's own for a type (DESIGN.md §4 ``keep.toml``, §5; #581).

A keep's own fields sit beside its theme's: declared in ``keep.toml`` so they travel with
the keep, stored by the core (``entity_field``, ``core/own_values.py``), never in a theme's
tables::

    [fields."books.book".pages_read]
    label = "Pages read"
    kind = "integer"
    search = "range"
    display = "duration"
    card = true
    column = true

This module is the definitions alone: what a field may be, and the checks a new or changed
one passes, and their ``keep.toml`` form. It reads no database and imports no theme code.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from tagalot.core.tomlio import toml_key, toml_str

OWN = "own:"
"""An own field's key where the core names fields (provenance, edits, merges, the page's
rows): ``own:<name>``, so it never clashes with a theme field of that name."""

KINDS = ("text", "integer", "number", "date", "yes/no", "link", "rating")
"""What an own field holds; a ``link`` is a web address, text that opens in the browser;
a ``rating`` is a number out of :data:`RATING_SCALE`, in half stars (#577)."""
RATING_SCALE = 5
"""What a rating of the user's own is out of: five stars, as a theme's ``rating=5``."""
SEARCHES = ("words", "range", "choice", "none")
"""How an own field is searched: its words, a range, a choice of its values, or not at
all (filters, #583)."""
DISPLAYS = ("bytes", "duration")
"""How a number reads (``3.0 MB``, ``3:25``), as theme fields' ``display``."""

NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
"""An own field's name: lowercase letters, digits, and ``_``, starting with a letter."""

_PYTHON_TYPES: Mapping[str, type] = {
    "text": str,
    "integer": int,
    "number": float,
    "date": date,
    "yes/no": bool,
    "link": str,
    "rating": float,
}
_SEARCHES_FOR: Mapping[str, tuple[str, ...]] = {
    "text": ("words", "choice", "none"),
    "integer": ("range", "choice", "none"),
    "number": ("range", "none"),
    "date": ("range", "none"),
    "yes/no": ("choice", "none"),
    "link": ("words", "none"),
    "rating": ("range", "none"),
}
"""The searches each kind may have, its default first."""


class OwnFieldError(ValueError):
    """A field can't be added or changed as asked; the message is meant for the user."""


@dataclass(frozen=True)
class OwnField:
    """One of a keep's own fields (``[fields."<type>".<name>]``)."""

    type_id: str
    """The type it belongs to (``"books.book"``)."""
    name: str
    """Its name, as :data:`NAME`; fixed once made (its values are stored under it)."""
    label: str
    kind: str = "text"
    """One of :data:`KINDS`; fixed once made."""
    search: str = ""
    """One of :data:`SEARCHES` (``""``: the kind's default, :func:`default_search`)."""
    display: str | None = None
    """For a number: one of :data:`DISPLAYS`, or None."""
    card: bool = False
    """A line on grid cards (#583)."""
    column: bool = False
    """A list column shown by default (#583)."""

    def __post_init__(self) -> None:
        if not self.search:
            object.__setattr__(self, "search", default_search(self.kind))

    @property
    def key(self) -> str:
        """``own:<name>``: the field's key where the core names fields (:data:`OWN`)."""
        return OWN + self.name

    @property
    def python_type(self) -> type:
        """The type its values have: ``str``, ``int``, ``float``, ``date``, or ``bool``."""
        return _PYTHON_TYPES[self.kind]

    @property
    def shown_as(self) -> str | None:
        """Its display format as theme fields have it: ``"url"`` for a link."""
        if self.kind == "rating":
            return f"stars/{RATING_SCALE}"
        return "url" if self.kind == "link" else self.display


OwnFields = Mapping[str, Mapping[str, OwnField]]
"""A keep's own fields: type id -> name -> field, in ``keep.toml``'s order."""


def rating_problem(value: float) -> str | None:
    """Why ``value`` can't be a rating of the user's own (out of :data:`RATING_SCALE`, in
    half stars), or None."""
    if not 0 <= value <= RATING_SCALE:
        return f"isn't a rating from 0 to {RATING_SCALE}"
    if value * 2 != int(value * 2):
        return "isn't in half stars"
    return None


def default_search(kind: str) -> str:
    """How a field of this kind is searched unless the user says otherwise."""
    return _SEARCHES_FOR.get(kind, ("none",))[0]


def searches_for(kind: str) -> tuple[str, ...]:
    """The searches a field of this kind may have, its default first."""
    return _SEARCHES_FOR.get(kind, ("none",))


def field_problem(own: OwnField) -> str | None:
    """What is wrong with a field on its own (its name, kind, search, display), or None.
    Meant for the user."""
    if not NAME.match(own.name):
        return (
            f"{own.name!r} can't be a field's name: use lowercase letters, digits, and _, "
            "starting with a letter"
        )
    if not own.label.strip():
        return f"The field {own.name!r} needs a label"
    if own.kind not in KINDS:
        return f"{own.kind!r} isn't a kind of field ({', '.join(KINDS)})"
    if own.search not in searches_for(own.kind):
        allowed = ", ".join(searches_for(own.kind))
        return f"A {own.kind} field can't be searched by {own.search!r} ({allowed})"
    if own.display is not None:
        if own.display not in DISPLAYS:
            return f"{own.display!r} isn't a display ({', '.join(DISPLAYS)})"
        if own.kind not in ("integer", "number"):
            return f"Only a number can show as {own.display!r}"
    return None


def clash(
    own: OwnField,
    theme_fields: Iterable[tuple[str, str]],
    title_label: str,
    others: Iterable[OwnField] = (),
) -> str | None:
    """Why ``own`` can't sit beside its type's theme fields (``(name, label)`` pairs), its
    title, and the type's other own fields, or None. Names and labels are compared case
    aside: a field named or labelled like one of the theme's is refused, since front-matter
    keys and filters find fields by either. Meant for the user."""
    names = {own.name.casefold(), own.label.strip().casefold()}
    for name, label in theme_fields:
        if names & {name.casefold(), label.casefold()}:
            return (
                f"{label!r} is already one of the theme's fields. To read a note's key into "
                "it, add a key alias (Notes tab) instead"
            )
    if names & {"title", title_label.casefold()}:
        return f"{own.label!r} is the item's {title_label.lower()}"
    for other in others:
        if other.name == own.name:
            continue
        if names & {other.name.casefold(), other.label.casefold()}:
            return f"There is already a field called {other.label!r}"
    return None


def name_for(label: str, taken: Iterable[str] = ()) -> str:
    """A field's name made from its label (``"Pages read"`` -> ``pages_read``), not one of
    ``taken``."""
    base = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")[:60] or "field"
    if not base[0].isalpha():
        base = f"field_{base}"[:60]
    used = set(taken)
    name, n = base, 2
    while name in used:
        name, n = f"{base}_{n}", n + 1
    return name


def parse_fields(table: Mapping[str, Any]) -> tuple[dict[str, dict[str, OwnField]], list[str]]:
    """``keep.toml``'s ``[fields]`` table as the keep's own fields, and what was left out
    and why (a field with a problem is left out, so the keep still opens)."""
    found: dict[str, dict[str, OwnField]] = {}
    problems: list[str] = []
    for type_id, fields in table.items():
        if not isinstance(fields, Mapping):
            problems.append(f'[fields."{type_id}"] must be a table of fields')
            continue
        for name, raw in fields.items():
            where = f'[fields."{type_id}".{name}]'
            if not isinstance(raw, Mapping):
                problems.append(f"{where} must be a table")
                continue
            try:
                own = _read(type_id, name, raw)
            except (TypeError, ValueError) as e:
                problems.append(f"{where}: {e}")
                continue
            problem = field_problem(own)
            if problem is not None:
                problems.append(f"{where}: {problem}")
                continue
            unknown = set(raw) - {"label", "kind", "search", "display", "card", "column"}
            if unknown:
                problems.append(f"{where}: ignoring {', '.join(sorted(unknown))}")
            found.setdefault(type_id, {})[name] = own
    return found, problems


def _read(type_id: str, name: str, raw: Mapping[str, Any]) -> OwnField:
    def text(key: str, default: str | None) -> str | None:
        value = raw.get(key, default)
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{key} must be text")
        return value

    def flag(key: str) -> bool:
        value = raw.get(key, False)
        if not isinstance(value, bool):
            raise TypeError(f"{key} must be true or false")
        return value

    return OwnField(
        type_id,
        name,
        (text("label", name) or name).strip(),
        text("kind", "text") or "text",
        text("search", "") or "",
        text("display", None) or None,
        flag("card"),
        flag("column"),
    )


def dump_fields(fields: OwnFields) -> list[str]:
    """``keep.toml``'s ``[fields."<type>".<name>]`` tables, as lines (no blank first)."""
    lines: list[str] = []
    for type_id, by_name in fields.items():
        for own in by_name.values():
            lines += ["", f"[fields.{toml_key(type_id)}.{toml_key(own.name)}]"]
            lines.append(f"label = {toml_str(own.label)}")
            lines.append(f"kind = {toml_str(own.kind)}")
            if own.search != default_search(own.kind):
                lines.append(f"search = {toml_str(own.search)}")
            if own.display is not None:
                lines.append(f"display = {toml_str(own.display)}")
            if own.card:
                lines.append("card = true")
            if own.column:
                lines.append("column = true")
    return lines


def with_field(fields: OwnFields, own: OwnField) -> dict[str, dict[str, OwnField]]:
    """``fields`` with ``own`` added, or put in place of the one of its name."""
    result = {t: dict(f) for t, f in fields.items()}
    result.setdefault(own.type_id, {})[own.name] = own
    return result


def without_field(fields: OwnFields, type_id: str, name: str) -> dict[str, dict[str, OwnField]]:
    """``fields`` without the type's field ``name`` (a type left with none goes too)."""
    result = {t: dict(f) for t, f in fields.items()}
    result.get(type_id, {}).pop(name, None)
    return {t: f for t, f in result.items() if f}


def changed(own: OwnField, **changes: Any) -> OwnField:
    """``own`` with its label, search, display, card, or column changed; its name and kind
    stay (its values are stored by them)."""
    if {"name", "kind", "type_id"} & set(changes):
        raise ValueError("A field's name, kind, and type stay as made")
    return replace(own, **changes)
