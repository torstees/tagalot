"""Linked places: one file or folder in several places (DESIGN.md §13 *Linked places*;
#598).

A place linked to another (``linked_place``) is the same file or folder, found at another
path: a symbolic link, a junction, a hard link, a shortcut, or a link a share shows as an
ordinary file. Each group of places has a **first place**, which the group's items link to
as any file; the others have no items of their own.

- **Scans** read a linked place in place mode (``IngestSession(place=...)``): the theme
  finds the group's items through it, and what its path adds (an author's or artist's
  folder as a container) is added to them, recorded in ``place_contains`` so it goes when
  the place stops giving it. Nothing a place's scan says removes anything, and its values
  are the first place's (the same file).
- **Pages and opening** list a file's places after it (``detail.role_files``), so a file
  opens from the first place that can be reached.
- **Linking** (:func:`tagalot.core.place_links.link_place`) folds an item the place had
  into the group's item of its type, as a merge (§13).
"""

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import Connection, Select, select

from tagalot.core.models import LinkedPlace, Resource

HOWS = frozenset({"symlink", "junction", "hardlink", "shortcut", "user", "inside"})
"""How a place can be linked (``linked_place.how``)."""

BATCH = 500


def linked_places() -> Select[tuple[int]]:
    """The ids of every linked place (not first places), for leaving them out of queries."""
    return select(LinkedPlace.resource_id)


def first_place(conn: Connection, resource_id: int) -> int:
    """The first place of the resource's group: itself when it isn't a linked place."""
    first = conn.scalar(select(LinkedPlace.first_id).where(LinkedPlace.resource_id == resource_id))
    return resource_id if first is None else first


def places_of(conn: Connection, first_ids: Iterable[int]) -> dict[int, list[int]]:
    """Each of these first places' linked places, by path."""
    ids = sorted(set(first_ids))
    found: dict[int, list[int]] = defaultdict(list)
    for start in range(0, len(ids), BATCH):
        rows = conn.execute(
            select(LinkedPlace.first_id, LinkedPlace.resource_id)
            .join(Resource, Resource.id == LinkedPlace.resource_id)
            .where(LinkedPlace.first_id.in_(ids[start : start + BATCH]))
            .order_by(Resource.root_id, Resource.relpath)
        )
        for first, place in rows:
            found[first].append(place)
    return dict(found)
