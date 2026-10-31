"""Real links, linked automatically (DESIGN.md §13 *Linked places*, #599).

The walk says which paths it reached through a link (``WalkEntry.target``, ``how``):
symbolic links and junctions, and the paths inside a linked folder, with the real paths
they have; and which files have several hard links (``file_id``). After a root's walk is
applied, :func:`sync_links` makes each a linked place of the resource at its real path,
when that is in one of the keep's roots and recorded there, and, for hard links, links the
names of one file found in the root to the first of them. A path linked this way that the
walk now finds as itself (the link replaced by a file or folder) is unlinked again.
"""

import logging
import os
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import Connection, select

from tagalot.core.models import EntityResource, LinkedFolder, LinkedPlace, Resource
from tagalot.core.place_links import PlaceError, link_place, unlink_place
from tagalot.core.places import first_place
from tagalot.core.scanner import WalkEntry
from tagalot.core.theme_schema import ThemeSchema

logger = logging.getLogger(__name__)

AUTOMATIC = frozenset({"symlink", "junction", "hardlink", "shortcut", "inside"})
"""Ways of linking the scan makes and unmakes itself; the user's links are left alone."""

BATCH = 500


@dataclass(frozen=True)
class LinkFact:
    """What the walk found about one path, as :func:`sync_links` takes it."""

    relpath: str
    target: str | None = None
    how: str | None = None
    file_id: tuple[int, int] | None = None


def link_facts(entries: Iterable[WalkEntry]) -> list[LinkFact]:
    """The walk's entries reached through a link or with several hard links."""
    return [
        LinkFact(e.relpath, e.target, e.how, e.file_id)
        for e in entries
        if e.target is not None or e.file_id is not None
    ]


def sync_links(
    conn: Connection,
    schema: ThemeSchema,
    root_id: str,
    facts: list[LinkFact],
    walked: Collection[str],
    roots: Mapping[str, str],
) -> int:
    """Link this root's paths the walk reached through links (see the module docstring).
    ``walked`` are the paths the walk saw; ``roots`` are this computer's paths of the
    keep's roots, by id. Returns how many places were linked or unlinked."""
    real_roots = sorted(
        ((_norm(os.path.realpath(path)), root) for root, path in roots.items()),
        key=lambda pair: -len(pair[0]),  # the deepest root first, for roots inside roots
    )
    here = _ids(conn, root_id, [f.relpath for f in facts])
    wanted: dict[int, tuple[int, str]] = {}
    for fact in facts:
        place = here.get(fact.relpath)
        if place is None or fact.target is None or fact.how is None:
            continue
        found = _where(fact.target, real_roots)
        if found is None:
            continue  # outside the keep's folders: #603
        first = _id(conn, *found)
        if first is not None and first != place:
            wanted[place] = (first, fact.how)
    for group in _hard_linked(facts).values():
        ids = sorted(i for i in (here.get(r) for r in group) if i is not None)
        if len(ids) < 2:
            continue
        first = _first_of(conn, ids)
        for place in ids:
            if place != first and place not in wanted:
                wanted[place] = (first, "hardlink")
    changed = 0
    for place, (first, how) in wanted.items():
        current = conn.execute(
            select(LinkedPlace.first_id, LinkedPlace.how).where(LinkedPlace.resource_id == place)
        ).first()
        if current is not None and current[0] == first_place(conn, first):
            continue
        if current is not None and current[1] not in AUTOMATIC:
            continue  # the user linked it elsewhere
        try:
            link_place(conn, schema, place, first, how)
        except PlaceError as e:
            logger.info("Not linking resource %s to %s: %s", place, first, e)
            continue
        changed += 1
    walked_set = set(walked)
    automatic = conn.execute(
        select(LinkedPlace.resource_id, Resource.relpath)
        .join(Resource, Resource.id == LinkedPlace.resource_id)
        .where(Resource.root_id == root_id, LinkedPlace.how.in_(sorted(AUTOMATIC)))
    ).all()
    folders = _linked_folders(conn, root_id)
    for place, relpath in automatic:
        if relpath in walked_set and place not in wanted:
            if any(relpath == f or relpath.startswith(f + "/") for f in folders):
                continue  # in folders the user linked (#601): not a link on disk
            unlink_place(conn, place)  # found as itself now: no longer a link
            changed += 1
    return changed


def _linked_folders(conn: Connection, root_id: str) -> list[str]:
    """This root's folders on either side of a linked folders pair (#601), whose paths
    are linked though the walk finds them as themselves."""
    others = conn.scalars(select(LinkedFolder.relpath).where(LinkedFolder.root_id == root_id))
    firsts = conn.scalars(
        select(LinkedFolder.first_relpath).where(LinkedFolder.first_root_id == root_id)
    )
    return [*others, *firsts]


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _where(target: str, real_roots: list[tuple[str, str]]) -> tuple[str, str] | None:
    """The root and relative POSIX path of a real path, if it is in one of the roots."""
    norm = _norm(target)
    for folder, root_id in real_roots:
        if norm == folder:
            return root_id, ""
        prefix = folder.rstrip(os.sep) + os.sep
        if norm.startswith(prefix):
            # Cut the original (not case-folded) path at the same length.
            inside = os.path.normpath(target)[len(prefix) :]
            return root_id, inside.replace(os.sep, "/")
    return None


def _ids(conn: Connection, root_id: str, relpaths: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for start in range(0, len(relpaths), BATCH):
        rows = conn.execute(
            select(Resource.relpath, Resource.id).where(
                Resource.root_id == root_id,
                Resource.relpath.in_(relpaths[start : start + BATCH]),
                Resource.parent_resource_id.is_(None),
            )
        )
        found.update({relpath: rid for relpath, rid in rows})
    return found


def _id(conn: Connection, root_id: str, relpath: str) -> int | None:
    """The resource at a path, matched ignoring case as the real path may differ in it."""
    exact = conn.scalar(
        select(Resource.id).where(
            Resource.root_id == root_id,
            Resource.relpath == relpath,
            Resource.parent_resource_id.is_(None),
        )
    )
    if exact is not None or os.path.normcase("A") == "A":
        return exact
    return conn.scalar(
        select(Resource.id)
        .where(
            Resource.root_id == root_id,
            Resource.relpath.ilike(
                relpath.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"), escape="\\"
            ),
            Resource.parent_resource_id.is_(None),
        )
        .limit(1)
    )


def _hard_linked(facts: list[LinkFact]) -> dict[tuple[int, int], list[str]]:
    groups: dict[tuple[int, int], list[str]] = defaultdict(list)
    for fact in facts:
        if fact.file_id is not None:
            groups[fact.file_id].append(fact.relpath)
    return {k: v for k, v in groups.items() if len(v) > 1}


def _first_of(conn: Connection, ids: list[int]) -> int:
    """The group's first place among the names of one file: one that already is a first
    place, else one with an item, else the first by id."""
    firsts = {first_place(conn, i) for i in ids} & set(ids)
    if len(firsts) == 1:
        return next(iter(firsts))
    with_items = conn.scalar(
        select(EntityResource.resource_id)
        .where(EntityResource.resource_id.in_(ids))
        .order_by(EntityResource.resource_id)
        .limit(1)
    )
    return with_items if with_items is not None else ids[0]
