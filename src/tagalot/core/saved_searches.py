"""Saved searches (DESIGN.md §8, §12; #127).

A saved search remembers a search page as the user left it: the page's own starting point
(``base``: a view's types, toggles, and the sort chosen), what its filter bar asked for
(``filters``: tags, text, field filters, an "Only" type, a "Within" item, and the two
toggles, as a :class:`SearchSpec` of their own), and how it was shown (``layout``, and
``grouped`` for Search all's sections). Opening it rebuilds the page and its chips, so
every chip can still be removed. It is stored in ``saved_search.definition`` as JSON.

Saving, replacing, renaming, and deleting are each one undo step (:class:`SavedChange`).
Names are unique, ignoring case.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, delete, insert, select, update

from tagalot.core.models import SavedSearch
from tagalot.core.search_spec import SearchSpec, SearchSpecError

DEFINITION_VERSION = 1
MAX_NAME = 200
LAYOUTS = ("list", "grid", "tree")


class SavedSearchError(ValueError):
    """A save, rename, or definition that can't be used; the message is for the user."""


@dataclass(frozen=True)
class SavedDefinition:
    """Everything needed to open a saved search again (see the module docstring)."""

    base: SearchSpec = field(default_factory=SearchSpec)
    filters: SearchSpec = field(default_factory=SearchSpec)
    """The filter bar: ``types`` holds the "Only" type, if any."""
    within_title: str = ""
    """The "Within" item's title when it was saved (the chip shows it)."""
    within_type: str = ""
    layout: str = "list"
    grouped: bool = False
    """Search all, shown in sections by type."""

    def to_json(self) -> dict[str, Any]:
        return {
            "version": DEFINITION_VERSION,
            "base": self.base.to_json(),
            "filters": self.filters.to_json(),
            "within_title": self.within_title,
            "within_type": self.within_type,
            "layout": self.layout,
            "grouped": self.grouped,
        }

    @classmethod
    def from_json(cls, data: Any) -> "SavedDefinition":
        """Read a stored definition. A bare search (an older or hand-made row) opens as
        a search of its own with no chips."""
        if not isinstance(data, Mapping):
            raise SavedSearchError("This saved search isn't readable.")
        try:
            if "base" not in data:
                return cls(base=SearchSpec.from_json(data))
            version = data.get("version", DEFINITION_VERSION)
            if not isinstance(version, int) or version > DEFINITION_VERSION:
                raise SavedSearchError("This search was saved by a newer version of Tagalot.")
            layout = data.get("layout", "list")
            return cls(
                base=SearchSpec.from_json(data["base"]),
                filters=SearchSpec.from_json(data.get("filters") or {}),
                within_title=str(data.get("within_title") or ""),
                within_type=str(data.get("within_type") or ""),
                layout=layout if layout in LAYOUTS else "list",
                grouped=bool(data.get("grouped", False)),
            )
        except SearchSpecError as e:
            raise SavedSearchError(str(e)) from None


Row = tuple[int, str, dict[str, Any]]
"""``(id, name, definition)``."""


@dataclass(frozen=True)
class SavedChange:
    """Everything needed to undo and redo one change to the saved searches."""

    label: str
    before: tuple[Row, ...]
    """The touched rows before (absent: it didn't exist)."""
    after: tuple[Row, ...]
    ids: tuple[int, ...]
    """The ids touched."""
    saved_id: int | None = None
    """The search saved, for the window to open."""


def list_saved(conn: Connection) -> list[Row]:
    """Every saved search, by name (ignoring case)."""
    rows = conn.execute(select(SavedSearch.id, SavedSearch.name, SavedSearch.definition))
    return sorted(((i, n, dict(d or {})) for i, n, d in rows), key=lambda r: r[1].casefold())


def find_by_name(conn: Connection, name: str) -> int | None:
    """The id of the saved search with this name (ignoring case), if any."""
    wanted = clean_name(name).casefold()
    for saved_id, saved_name, _ in list_saved(conn):
        if saved_name.casefold() == wanted:
            return saved_id
    return None


def save_search(
    conn: Connection,
    name: str,
    definition: SavedDefinition,
    *,
    replace: int | None = None,
) -> SavedChange:
    """Save a search under ``name``, as a new one, or over ``replace`` (an update keeps its
    id, so its page and remembered layout stay). Another search with the same name is
    refused unless it is the one replaced."""
    name = clean_name(name)
    taken = find_by_name(conn, name)
    if taken is not None and taken != replace:
        raise SavedSearchError(f"There's already a saved search named {name!r}.")
    data = definition.to_json()
    if replace is not None:
        before = _rows(conn, [replace])
        if not before:
            raise SavedSearchError("That saved search no longer exists.")
        conn.execute(
            update(SavedSearch).where(SavedSearch.id == replace).values(name=name, definition=data)
        )
        saved_id, label = replace, f"Update the saved search {name!r}"
    else:
        before = ()
        saved_id = int(
            conn.execute(
                insert(SavedSearch).values(name=name, definition=data).returning(SavedSearch.id)
            ).scalar_one()
        )
        label = f"Save the search {name!r}"
    return SavedChange(label, before, _rows(conn, [saved_id]), (saved_id,), saved_id)


def rename_search(conn: Connection, saved_id: int, name: str) -> SavedChange:
    name = clean_name(name)
    before = _rows(conn, [saved_id])
    if not before:
        raise SavedSearchError("That saved search no longer exists.")
    taken = find_by_name(conn, name)
    if taken is not None and taken != saved_id:
        raise SavedSearchError(f"There's already a saved search named {name!r}.")
    conn.execute(update(SavedSearch).where(SavedSearch.id == saved_id).values(name=name))
    old = before[0][1]
    return SavedChange(
        f"Rename the saved search {old!r} to {name!r}",
        before,
        _rows(conn, [saved_id]),
        (saved_id,),
    )


def delete_search(conn: Connection, saved_id: int) -> SavedChange:
    before = _rows(conn, [saved_id])
    if not before:
        raise SavedSearchError("That saved search no longer exists.")
    conn.execute(delete(SavedSearch).where(SavedSearch.id == saved_id))
    return SavedChange(f"Delete the saved search {before[0][1]!r}", before, (), (saved_id,))


def restore_saved(conn: Connection, change: SavedChange, *, forward: bool) -> None:
    """Undo (``forward=False``) or redo a change: the touched rows become as they were."""
    rows = change.after if forward else change.before
    conn.execute(delete(SavedSearch).where(SavedSearch.id.in_(change.ids)))
    if rows:
        conn.execute(
            insert(SavedSearch),
            [{"id": i, "name": n, "definition": d} for i, n, d in rows],
        )


def clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise SavedSearchError("A saved search needs a name.")
    if len(cleaned) > MAX_NAME:
        raise SavedSearchError(f"Keep the name under {MAX_NAME} characters.")
    return cleaned


def _rows(conn: Connection, ids: Sequence[int]) -> tuple[Row, ...]:
    rows = conn.execute(
        select(SavedSearch.id, SavedSearch.name, SavedSearch.definition).where(
            SavedSearch.id.in_(ids)
        )
    )
    return tuple((i, n, dict(d or {})) for i, n, d in rows)
