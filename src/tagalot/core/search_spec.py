"""The search model: ``SearchSpec`` and its JSON form (DESIGN.md §8).

A :class:`SearchSpec` is an immutable value: tuples rather than lists, so it is hashable and
can key result caches. Its JSON form (used by saved searches and view state) is versioned.
"""

import enum
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

SPEC_VERSION = 1

Scalar = str | int | float | bool | date | datetime
"""A field value in a filter. Dates and datetimes survive the JSON round trip."""


class SearchSpecError(ValueError):
    """A search definition is invalid; the message is suitable for showing to the user."""


def clean_search_text(text: str) -> str:
    """Make search-box text safe to query: control characters (NUL, escapes, …) become
    spaces, unpaired surrogates (which can't be encoded) are dropped, and the ends trimmed.

    FTS5 treats NUL as the end of its query string, so an embedded NUL would otherwise make
    the search fail.
    """
    cleaned = []
    for c in text:
        if unicodedata.category(c) == "Cc":
            cleaned.append(" ")
        elif not 0xD800 <= ord(c) <= 0xDFFF:
            cleaned.append(c)
    return "".join(cleaned).strip()


class TextMatch(enum.Enum):
    """How a :class:`TextFilter` compares; both ignore case."""

    CONTAINS = "contains"
    """Anywhere in the value: "road" matches "Abbey Road"."""
    STARTS_WITH = "starts_with"
    """At the start of the whole value: "the" matches "The Beatles", not "Abbey Road: The"."""


@dataclass(frozen=True)
class TextFilter:
    """Compare a ``search="text"`` field with ``text``, ignoring case."""

    field: str
    text: str
    match: TextMatch = TextMatch.CONTAINS


@dataclass(frozen=True)
class RangeFilter:
    """``low <= field <= high``; either bound may be ``None`` (open). For ``search="range"``."""

    field: str
    low: Scalar | None = None
    high: Scalar | None = None


@dataclass(frozen=True)
class ChoiceFilter:
    """The field equals one of ``values``. For ``search="choice"`` fields."""

    field: str
    values: tuple[Scalar, ...]


FieldFilter = TextFilter | RangeFilter | ChoiceFilter


@dataclass(frozen=True)
class SortKey:
    field: str
    """``title``, ``created_at``, ``updated_at``, or a field valid for every type in scope."""
    descending: bool = False


@dataclass(frozen=True)
class SearchSpec:
    """A search (DESIGN.md §8)."""

    types: tuple[str, ...] = ()
    """Entity types in scope, e.g. ``("music.album", "music.song")``; empty = all types."""
    include: tuple[int, ...] = ()
    """Tag ids. Each is expanded to its subtree and every one must match (AND of ORs)."""
    exclude: tuple[int, ...] = ()
    """Tag ids, expanded and merged; matching any of them removes the item."""
    fields: tuple[FieldFilter, ...] = ()
    within: int | None = None
    """Restrict to descendants of this entity (drill-down, container pages)."""
    text: str | None = None
    """Matches titles, text-search fields, and ``extra`` values."""
    inherit_tags: bool = False
    """Tags on ancestors count as the entity's own, for include and exclude."""
    show_contained: bool = False
    """Also list descendants of matching containers."""
    aggregate_up: bool = False
    """A container matches if any of its descendants does."""
    nest: bool = False
    """Leave out matches that one of their containers' matches holds (the tree layout lists
    them under it instead). Ignored with ``show_contained``."""
    sort: tuple[SortKey, ...] = field(default=(SortKey("title"),))

    def __post_init__(self) -> None:
        text = clean_search_text(self.text) if self.text is not None else None
        object.__setattr__(self, "text", text or None)
        for name in ("include", "exclude"):
            ids = getattr(self, name)
            if not all(isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in ids):
                raise SearchSpecError(f"{name} must list positive tag ids, not {ids!r}")
        within = self.within
        if within is not None and (
            not isinstance(within, int) or isinstance(within, bool) or within <= 0
        ):
            raise SearchSpecError(f"within must be a positive entity id, not {self.within!r}")

    # --- JSON ---

    def to_json(self) -> dict[str, Any]:
        """A JSON-compatible dict; see :meth:`from_json`."""
        return {
            "version": SPEC_VERSION,
            "types": list(self.types),
            "include": list(self.include),
            "exclude": list(self.exclude),
            "fields": [_filter_to_json(f) for f in self.fields],
            "within": self.within,
            "text": self.text,
            "inherit_tags": self.inherit_tags,
            "show_contained": self.show_contained,
            "aggregate_up": self.aggregate_up,
            "nest": self.nest,
            "sort": [{"field": k.field, "descending": k.descending} for k in self.sort],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "SearchSpec":
        """Rebuild a spec. Missing keys take their defaults and unknown keys are ignored, so
        older and newer builds can share saved searches; a newer ``version`` is refused."""
        if not isinstance(data, Mapping):
            raise SearchSpecError("A saved search must be a JSON object.")
        version = data.get("version", SPEC_VERSION)
        if not isinstance(version, int) or version > SPEC_VERSION:
            raise SearchSpecError(
                f"This search was saved by a newer version of Tagalot (format {version})."
            )
        defaults = cls()
        try:
            return cls(
                types=tuple(_strings(data.get("types", []), "types")),
                include=tuple(_list(data.get("include", []), "include")),
                exclude=tuple(_list(data.get("exclude", []), "exclude")),
                fields=tuple(_filter_from_json(f) for f in _list(data.get("fields", []), "fields")),
                within=data.get("within"),
                text=_optional_str(data.get("text"), "text"),
                inherit_tags=_bool(data, "inherit_tags", defaults.inherit_tags),
                show_contained=_bool(data, "show_contained", defaults.show_contained),
                aggregate_up=_bool(data, "aggregate_up", defaults.aggregate_up),
                nest=_bool(data, "nest", defaults.nest),
                sort=tuple(_sort_from_json(k) for k in _list(data.get("sort"), "sort"))
                if "sort" in data
                else defaults.sort,
            )
        except (KeyError, TypeError) as e:
            raise SearchSpecError(f"Invalid saved search: {e}") from e

    def dumps(self) -> str:
        """Compact JSON text (for ``saved_search.definition`` and ``ui_state.json``)."""
        return json.dumps(self.to_json(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def loads(cls, text: str) -> "SearchSpec":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise SearchSpecError(f"A saved search is not valid JSON: {e}") from e
        return cls.from_json(data)


# --- JSON helpers ---


def _scalar_to_json(value: Scalar | None) -> Any:
    # datetime is a subclass of date, so test it first.
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    return value


def _scalar_from_json(value: Any, where: str) -> Scalar | None:
    if isinstance(value, dict):
        try:
            if set(value) == {"$datetime"}:
                return datetime.fromisoformat(value["$datetime"])
            if set(value) == {"$date"}:
                return date.fromisoformat(value["$date"])
        except (TypeError, ValueError) as e:
            raise SearchSpecError(f"{where}: invalid date {value!r}") from e
        raise SearchSpecError(f"{where}: unexpected value {value!r}")
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise SearchSpecError(f"{where}: unexpected value {value!r}")


def _filter_to_json(f: FieldFilter) -> dict[str, Any]:
    match f:
        case TextFilter():
            return {"kind": "text", "field": f.field, "text": f.text, "match": f.match.value}
        case RangeFilter():
            return {
                "kind": "range",
                "field": f.field,
                "low": _scalar_to_json(f.low),
                "high": _scalar_to_json(f.high),
            }
        case ChoiceFilter():
            return {
                "kind": "choice",
                "field": f.field,
                "values": [_scalar_to_json(v) for v in f.values],
            }


def _filter_from_json(data: Any) -> FieldFilter:
    if not isinstance(data, dict) or not isinstance(data.get("field"), str):
        raise SearchSpecError(f"Invalid field filter {data!r}")
    name, kind = data["field"], data.get("kind")
    where = f"filter on {name!r}"
    if kind == "text":
        text = data.get("text")
        if not isinstance(text, str):
            raise SearchSpecError(f"{where}: 'text' must be text")
        try:
            match = TextMatch(data.get("match", TextMatch.CONTAINS.value))
        except ValueError as e:
            raise SearchSpecError(f"{where}: unknown match {data.get('match')!r}") from e
        return TextFilter(name, text, match)
    if kind == "range":
        return RangeFilter(
            name,
            _scalar_from_json(data.get("low"), where),
            _scalar_from_json(data.get("high"), where),
        )
    if kind == "choice":
        values = tuple(_scalar_from_json(v, where) for v in _list(data.get("values"), where))
        if any(v is None for v in values):
            raise SearchSpecError(f"{where}: choices can't be empty")
        return ChoiceFilter(name, values)  # type: ignore[arg-type]
    raise SearchSpecError(f"{where}: unknown kind {kind!r}")


def _sort_from_json(data: Any) -> SortKey:
    if not isinstance(data, dict) or not isinstance(data.get("field"), str):
        raise SearchSpecError(f"Invalid sort key {data!r}")
    return SortKey(data["field"], _bool(data, "descending", False))


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise SearchSpecError(f"{where} must be a list")
    return value


def _strings(value: Any, where: str) -> list[str]:
    items = _list(value, where)
    if not all(isinstance(v, str) for v in items):
        raise SearchSpecError(f"{where} must be a list of text")
    return items


def _optional_str(value: Any, where: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise SearchSpecError(f"{where} must be text")
    return value


def _bool(data: Mapping[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise SearchSpecError(f"{key} must be true or false")
    return value
