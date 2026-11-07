"""Links from a note's mirrored fields (DESIGN.md §9 *Notes*, #645).

A field that mirrors a link (``field(..., mirrors=...)``) names the items an item is in, or
related to: a movie's **Cast** (``mirrors="cast"``) or **Set** (``mirrors=Collection``).
When a note gives such a field, :func:`link_note_names` changes the links **by
difference**: the item is put in, or related to, the items the note names now and didn't
before, and taken out of, or unrelated from, those it named before and doesn't now. Links
the note never named (a folder's collection, an .nfo's cast, the user's) are left alone.
Each name is found or made with the theme's ``item_named``, so a note and an edit agree on
one item. It runs through the ingest session, so what the user did by hand stays.
"""

from typing import Any

from sqlalchemy import select

from tagalot.core.ingest import IngestSession
from tagalot.core.linked_fields import names_in
from tagalot.core.models import Entity, EntityContains
from tagalot.themes.api import EntityRef, FieldInfo


def link_note_names(
    ctx: IngestSession, entity: EntityRef, info: FieldInfo, old: Any, new: Any
) -> None:
    """Change ``entity``'s links for mirrored field ``info``, whose note value went from
    ``old`` to ``new`` (each the field's text, or ``None``)."""
    mirror = info.spec.mirrors
    if mirror is None:
        return
    before = {n.casefold(): n for n in names_in(old if isinstance(old, str) else None)}
    after = {n.casefold(): n for n in names_in(new if isinstance(new, str) else None)}
    gone = {key for key in before if key not in after}
    added = [name for key, name in after.items() if key not in before]
    if not gone and not added:
        return
    theme = ctx.schema.theme()
    if isinstance(mirror, type):
        parent_type = ctx.schema.theme.type_id_of(mirror)
        parents = ctx.conn.execute(
            select(Entity.id, Entity.title)
            .join(EntityContains, EntityContains.parent_id == Entity.id)
            .where(EntityContains.child_id == entity.id, Entity.type == parent_type)
        )
        for parent_id, title in parents.all():
            if title.casefold() in gone:
                ctx.uncontain(EntityRef(parent_id, parent_type), entity)
        for name in added:
            ctx.contain(theme.item_named(mirror, name, ctx), entity)
        return
    rel = ctx.schema.relationships[mirror].relationship
    item_is_a = entity.type == ctx.schema.theme.type_id_of(rel.a)
    other = rel.b if item_is_a else rel.a

    def pair(them: EntityRef) -> tuple[EntityRef, EntityRef]:
        return (entity, them) if item_is_a else (them, entity)

    for them in ctx.related(mirror, entity):
        if ctx.get(them).title.casefold() in gone:
            ctx.unrelate(mirror, *pair(them))
    for name in added:
        ctx.relate(mirror, *pair(theme.item_named(other, name, ctx)))
