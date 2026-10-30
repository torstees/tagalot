"""Linking places (DESIGN.md §13 *Linked places*, #598): see :mod:`tagalot.core.places`."""

from sqlalchemy import Connection, delete, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core.merge import merge_items
from tagalot.core.models import (
    Entity,
    EntityContains,
    EntityResource,
    LinkedPlace,
    PlaceContains,
    Resource,
)
from tagalot.core.places import HOWS, first_place
from tagalot.core.theme_schema import ThemeSchema


class PlaceError(ValueError):
    """A link that can't be made; the message is for the user."""


def link_place(
    conn: Connection, schema: ThemeSchema, place_id: int, first_id: int, how: str
) -> None:
    """Link ``place_id`` as a place of ``first_id``'s group (its first place's, when
    ``first_id`` is itself a linked place). Places linked to ``place_id`` join the group
    too. An item the place was a file of is merged into the group's item of its type in
    that role, keeping the group's item (§13: its values win where both were set by hand);
    one with no such item just loses the file. The place is read again at the next scan,
    to file the group's items where its path says."""
    if how not in HOWS:
        raise ValueError(f"not a way of linking places: {how!r}")
    first = first_place(conn, first_id)
    if first == place_id:
        raise PlaceError("A file can't be linked to itself.")
    kinds = conn.execute(
        select(Resource.id, Resource.kind).where(Resource.id.in_([place_id, first]))
    ).all()
    if len(kinds) != 2:
        raise PlaceError("That file is no longer in the keep.")
    if kinds[0][1] != kinds[1][1]:
        raise PlaceError("A file can only be linked to a file, and a folder to a folder.")
    conn.execute(update(LinkedPlace).where(LinkedPlace.first_id == place_id).values(first_id=first))
    _fold_items(conn, schema, place_id, first)
    conn.execute(
        sqlite_insert(LinkedPlace)
        .values(resource_id=place_id, first_id=first, how=how)
        .on_conflict_do_update(
            index_elements=[LinkedPlace.resource_id], set_={"first_id": first, "how": how}
        )
    )
    conn.execute(update(Resource).where(Resource.id == place_id).values(ingested_at=None))


def _fold_items(conn: Connection, schema: ThemeSchema, place_id: int, first: int) -> None:
    """The place's items become the group's: each is merged into the item of its type
    linked to the first place in the same role, and the place stops being a file of any.
    The containers the group's item gains from a merged item are recorded as the place's,
    as the place's scans would have given them."""
    types = {t.type_id for t in schema.entities.values()}
    rows = conn.execute(
        select(EntityResource.entity_id, EntityResource.role, Entity.type)
        .join(Entity, Entity.id == EntityResource.entity_id)
        .where(EntityResource.resource_id == place_id)
        .order_by(EntityResource.entity_id, EntityResource.role)
    ).all()
    merged: set[int] = set()
    for entity_id, role, type_id in rows:
        if entity_id in merged or type_id not in types:
            continue
        keep = conn.scalar(
            select(EntityResource.entity_id)
            .join(Entity, Entity.id == EntityResource.entity_id)
            .where(
                EntityResource.resource_id == first,
                EntityResource.role == role,
                Entity.type == type_id,
            )
            .order_by(EntityResource.by_user, EntityResource.entity_id)
            .limit(1)
        )
        if keep is not None and keep != entity_id:
            had = _parents(conn, keep)
            merge_items(conn, schema, keep, [entity_id])
            merged.add(entity_id)
            gained = _parents(conn, keep) - had
            if gained:  # the containers it came with are the place's (it gives them)
                conn.execute(
                    insert(PlaceContains).prefix_with("OR IGNORE"),
                    [
                        {"resource_id": place_id, "parent_id": p, "child_id": keep}
                        for p in sorted(gained)
                    ],
                )
    conn.execute(delete(EntityResource).where(EntityResource.resource_id == place_id))


def _parents(conn: Connection, entity_id: int) -> set[int]:
    return set(
        conn.scalars(select(EntityContains.parent_id).where(EntityContains.child_id == entity_id))
    )
