"""Items whose files are all skipped (#526, DESIGN.md §6 *Scanning*).

A file a scan now leaves out (a Skip pattern, an extension or kind of folder the theme no
longer takes, an archive member no longer read) is flagged ``skipped`` and keeps its item,
so unskipping it brings the item back with its tags. Until then, an item with files and
none of them still read is outside what the keep is interested in, and is hidden from lists,
searches, and item pages. Items without files (an artist the theme made, an item the user
made) aren't affected.
"""

from sqlalchemy import Select, exists, false, select, true
from sqlalchemy.orm import aliased

from tagalot.core.models import EntityResource, Resource


def skipped_items() -> Select[int]:
    """Ids of entities that have files, all of them skipped. Built from the skipped files,
    which are few (found through their partial index), each item checked for a file that is
    still read."""
    skipped = select(Resource.id).where(Resource.skipped == true())
    link = aliased(EntityResource)
    other = aliased(Resource)
    still_read = (
        select(link.entity_id)
        .join(other, other.id == link.resource_id)
        .where(link.entity_id == EntityResource.entity_id, other.skipped == false())
    )
    return (
        select(EntityResource.entity_id)
        .where(EntityResource.resource_id.in_(skipped), ~exists(still_read))
        .distinct()
    )
