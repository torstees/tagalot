"""Linked folders: two folders that are one (DESIGN.md §13 *Linked places*, #601).

A share that resolves links itself shows a linked folder as an ordinary folder, so its
files look like copies. :func:`suggest_linked_folders` finds folders with the same name
whose files match (by name, size, and fingerprint), and :func:`folder_pair` previews a
pair: what matches, what differs, and what is on one side only. Files that differ block
linking (:func:`link_folders`), unless reading them whole found them the same after all
(:func:`confirm_same`: a file changing while it was read, or a network glitch).

Linking records the pair (``linked_folder``) and links what's inside by path, the second
folder's files and folders as places of the first's (``how`` ``inside``): one undo step,
a :class:`~tagalot.core.place_links.PlaceChange`. A scan then links a file that turns up
in both later, when they match (:func:`sync_linked_folders`); one that differs is listed
in Triage (:func:`folder_differences`).
"""

import logging
import posixpath
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass

from sqlalchemy import Connection, func, insert, or_, select

from tagalot.core.dedupe import DuplicateFile, DuplicateGroup, Verifier
from tagalot.core.models import (
    LinkedFolder,
    LinkedPlace,
    Resource,
    ResourceKind,
    ResourceStatus,
    Root,
)
from tagalot.core.place_links import (
    FolderRow,
    PlaceChange,
    PlaceError,
    _link,
    _rows,
    _touched,
    folder_rows,
)
from tagalot.core.roots import local_path
from tagalot.core.theme_schema import ThemeSchema

logger = logging.getLogger(__name__)

MAX_GROUP = 20
"""Files sharing a fingerprint that suggestions look at; more is likely a common file (an
empty one, a licence) that says nothing about where folders are linked."""
BATCH = 500


@dataclass(frozen=True)
class Folder:
    """A folder in one of the keep's roots."""

    root_id: str
    root_name: str
    relpath: str

    @property
    def name(self) -> str:
        return posixpath.basename(self.relpath)

    @property
    def shown(self) -> str:
        return f"{self.root_name}/{self.relpath}"


@dataclass(frozen=True)
class PairedPath:
    """A path found in both folders: the resources there."""

    inside: str
    """Its path inside the folders (``""`` for the folders themselves)."""
    first_id: int
    other_id: int
    linked: bool = False
    """Already places of one file."""


@dataclass(frozen=True)
class FolderPair:
    """Two folders, and how what's inside them compares."""

    first: Folder
    other: Folder
    same: tuple[PairedPath, ...] = ()
    """Files that match (and folders in both): linked by :func:`link_folders`."""
    differ: tuple[PairedPath, ...] = ()
    """Files whose size or fingerprint differ (or a file in one and a folder in the other)."""
    only_first: tuple[str, ...] = ()
    only_other: tuple[str, ...] = ()

    @property
    def files_same(self) -> int:
        """Matching paths, the folders themselves left out."""
        return sum(1 for p in self.same if p.inside)


@dataclass(frozen=True)
class _Entry:
    id: int
    kind: ResourceKind
    size: int | None
    fingerprint: bytes | None


def suggest_linked_folders(conn: Connection, max_group: int = MAX_GROUP) -> list[FolderPair]:
    """Folders with the same name holding identical files of the same name, not linked
    already, the most matches first. For each two identical files, the pair is the
    highest folders up from them whose names still match (``Bundle/Book`` and
    ``Authors/Jane/Book`` give ``Book``)."""
    usable = (
        Resource.kind == ResourceKind.FILE,
        Resource.fingerprint.is_not(None),
        Resource.size > 0,
        Resource.status != ResourceStatus.MISSING,
        Resource.skipped.is_(False),
        Resource.parent_resource_id.is_(None),
        Resource.id.not_in(select(LinkedPlace.resource_id)),
    )
    shared = (
        select(Resource.fingerprint)
        .where(*usable)
        .group_by(Resource.fingerprint)
        .having(func.count() > 1, func.count() <= max_group)
    )
    groups: dict[bytes | None, list[tuple[str, str]]] = defaultdict(list)
    for fingerprint, root_id, relpath in conn.execute(
        select(Resource.fingerprint, Resource.root_id, Resource.relpath)
        .where(*usable, Resource.fingerprint.in_(shared))
        .order_by(Resource.fingerprint, Resource.root_id, Resource.relpath)
    ):
        groups[fingerprint].append((root_id, relpath))
    names = _root_names(conn)
    linked = {
        frozenset({(a, b), (c, d)})
        for a, b, c, d in conn.execute(
            select(
                LinkedFolder.root_id,
                LinkedFolder.relpath,
                LinkedFolder.first_root_id,
                LinkedFolder.first_relpath,
            )
        )
    }
    candidates: dict[tuple[Folder, Folder], None] = {}
    for files in groups.values():
        for i, (root_a, path_a) in enumerate(files):
            for root_b, path_b in files[i + 1 :]:
                if posixpath.basename(path_a).casefold() != posixpath.basename(path_b).casefold():
                    continue
                top = _common_top(path_a, path_b)
                if top is None:
                    continue
                a = Folder(root_a, names.get(root_a, root_a), top[0])
                b = Folder(root_b, names.get(root_b, root_b), top[1])
                if root_a == root_b and (
                    _within(a.relpath, b.relpath) or _within(b.relpath, a.relpath)
                ):
                    continue
                if frozenset({(a.root_id, a.relpath), (b.root_id, b.relpath)}) in linked:
                    continue
                first, other = sorted((a, b), key=_order)
                candidates[first, other] = None
    pairs = [folder_pair(conn, first, other) for first, other in candidates]
    pairs.sort(key=lambda p: (-p.files_same, p.first.name.casefold(), _order(p.first)))
    return pairs


def _order(folder: Folder) -> tuple[str, str, str]:
    return folder.root_name.casefold(), folder.relpath.casefold(), folder.root_id


def _common_top(path_a: str, path_b: str) -> tuple[str, str] | None:
    """The highest folders above two files whose names match all the way down."""
    a, b = path_a.split("/")[:-1], path_b.split("/")[:-1]
    k = 0
    while k < min(len(a), len(b)) and a[-1 - k].casefold() == b[-1 - k].casefold():
        k += 1
    if k == 0:
        return None
    return "/".join(a[: len(a) - k + 1]), "/".join(b[: len(b) - k + 1])


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "/")


def _root_names(conn: Connection) -> dict[str, str]:
    return {i: n for i, n in conn.execute(select(Root.id, Root.name))}


def folder_of(conn: Connection, root_id: str, relpath: str) -> Folder:
    """A folder, with its root's name."""
    return Folder(root_id, _root_names(conn).get(root_id, root_id), relpath)


def folder_pair(conn: Connection, first: Folder, other: Folder) -> FolderPair:
    """How what's inside two folders compares, by path (see :class:`FolderPair`)."""
    a = _inside(conn, first.root_id, first.relpath)
    b = _inside(conn, other.root_id, other.relpath)
    both = sorted(a.keys() & b.keys())
    groups = _firsts(conn, [a[p].id for p in both] + [b[p].id for p in both])
    same: list[PairedPath] = []
    differ: list[PairedPath] = []
    for inside in both:
        x, y = a[inside], b[inside]
        linked = groups.get(x.id, x.id) == groups.get(y.id, y.id)
        paired = PairedPath(inside, x.id, y.id, linked)
        (same if linked or _same(x, y) else differ).append(paired)
    return FolderPair(
        first,
        other,
        tuple(same),
        tuple(differ),
        tuple(sorted(a.keys() - b.keys())),
        tuple(sorted(b.keys() - a.keys())),
    )


def _same(x: _Entry, y: _Entry) -> bool:
    if x.kind != y.kind:
        return False
    if x.kind is ResourceKind.DIR:
        return True
    return x.fingerprint is not None and (x.size, x.fingerprint) == (y.size, y.fingerprint)


def _inside(conn: Connection, root_id: str, relpath: str) -> dict[str, _Entry]:
    """The resources in a folder (and the folder itself, if recorded), by path inside it."""
    prefix = relpath + "/"
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        select(Resource.id, Resource.relpath, Resource.kind, Resource.size, Resource.fingerprint)
        .where(
            Resource.root_id == root_id,
            or_(Resource.relpath == relpath, Resource.relpath.like(escaped + "%", escape="\\")),
            Resource.parent_resource_id.is_(None),
            Resource.status != ResourceStatus.MISSING,
            Resource.skipped.is_(False),
        )
        .order_by(Resource.relpath)
    )
    return {
        path[len(prefix) :] if path != relpath else "": _Entry(rid, kind, size, fp)
        for rid, path, kind, size, fp in rows
    }


def _firsts(conn: Connection, ids: Iterable[int]) -> dict[int, int]:
    """The first place of each of these resources that is a linked place."""
    wanted = sorted(set(ids))
    found: dict[int, int] = {}
    for start in range(0, len(wanted), BATCH):
        found.update(
            {
                place: first
                for place, first in conn.execute(
                    select(LinkedPlace.resource_id, LinkedPlace.first_id).where(
                        LinkedPlace.resource_id.in_(wanted[start : start + BATCH])
                    )
                )
            }
        )
    return found


def link_folders(
    conn: Connection,
    schema: ThemeSchema,
    first: Folder,
    other: Folder,
    confirmed: Collection[tuple[int, int]] = (),
) -> PlaceChange:
    """Link ``other`` as one with ``first``: record the pair, and make each path inside
    ``other`` that matches one in ``first`` a place of it, its item merged into the
    first's as Merge does with its defaults. Files that differ refuse it, except those in
    ``confirmed`` (``(first id, other id)``: read whole and found the same). One undo
    step."""
    if (first.root_id, first.relpath) == (other.root_id, other.relpath):
        raise PlaceError("A folder can't be linked to itself.")
    pair = folder_pair(conn, first, other)
    allowed = set(confirmed)
    blocking = [p for p in pair.differ if (p.first_id, p.other_id) not in allowed]
    if blocking:
        n = len(blocking)
        raise PlaceError(
            f"{n:,} {'file differs' if n == 1 else 'files differ'} between these folders, so "
            "they may be copies rather than one folder. Check again reads them whole."
        )
    key = (other.root_id, other.relpath)
    if conn.execute(
        select(LinkedFolder.root_id).where(
            LinkedFolder.root_id == other.root_id, LinkedFolder.relpath == other.relpath
        )
    ).first():
        raise PlaceError(f"{other.shown} is linked to a folder already.")
    todo = [p for p in (*pair.same, *pair.differ) if not p.linked]
    touched = _touched(conn, [i for p in todo for i in (p.first_id, p.other_id)])
    before = _rows(conn, touched)
    folders_before = folder_rows(conn, [key])
    conn.execute(
        insert(LinkedFolder).values(
            root_id=other.root_id,
            relpath=other.relpath,
            first_root_id=first.root_id,
            first_relpath=first.relpath,
        )
    )
    merges = []
    for paired in todo:
        try:
            merges.extend(_link(conn, schema, paired.other_id, paired.first_id, "inside"))
        except PlaceError as e:
            logger.info("Not linking %s inside %s: %s", paired.inside, other.shown, e)
    return PlaceChange(
        f"Link folders {other.name}",
        tuple(touched),
        before,
        _rows(conn, touched),
        tuple(merges),
        folders=(key,),
        folders_before=folders_before,
        folders_after=folder_rows(conn, [key]),
    )


def confirm_same(
    conn: Connection,
    root_path: Callable[[str], str | None],
    verifier: Verifier,
    pairs: Iterable[PairedPath],
) -> set[tuple[int, int]]:
    """**Check again**: read each pair's two files whole; the pairs found the same after
    all, as ``(first id, other id)``. Does I/O: call it from a worker."""
    wanted = list(pairs)
    ids = sorted({i for p in wanted for i in (p.first_id, p.other_id)})
    names = _root_names(conn)
    files: dict[int, DuplicateFile] = {}
    for start in range(0, len(ids), BATCH):
        for row in conn.execute(
            select(
                Resource.id,
                Resource.root_id,
                Resource.relpath,
                Resource.size,
                Resource.mtime_ns,
                Resource.status,
                Resource.kind,
            ).where(Resource.id.in_(ids[start : start + BATCH]))
        ):
            if row.kind is not ResourceKind.FILE:
                continue
            base = root_path(row.root_id)
            files[row.id] = DuplicateFile(
                row.id,
                row.root_id,
                names.get(row.root_id, row.root_id),
                row.relpath,
                row.size or 0,
                row.mtime_ns,
                row.status,
                local_path(base, row.relpath) if base is not None else None,
            )
    found: set[tuple[int, int]] = set()
    for paired in wanted:
        a, b = files.get(paired.first_id), files.get(paired.other_id)
        if a is None or b is None:
            continue
        result = verifier.verify_group(DuplicateGroup(b"", a.size, (a, b)))
        if result.confirmed:
            found.add((paired.first_id, paired.other_id))
    return found


def _linked_folders(conn: Connection, root_id: str | None = None) -> list[FolderRow]:
    query = select(
        LinkedFolder.root_id,
        LinkedFolder.relpath,
        LinkedFolder.first_root_id,
        LinkedFolder.first_relpath,
    ).order_by(LinkedFolder.root_id, LinkedFolder.relpath)
    if root_id is not None:
        query = query.where(
            or_(LinkedFolder.root_id == root_id, LinkedFolder.first_root_id == root_id)
        )
    return [(a, b, c, d) for a, b, c, d in conn.execute(query)]


def sync_linked_folders(
    conn: Connection, schema: ThemeSchema, root_id: str, resource_ids: Collection[int]
) -> int:
    """After a scan of ``root_id``: link each of its new or changed paths (``resource_ids``)
    inside a linked folder to the same path in the folder it is linked with, when that is
    there and matches (a file that differs is left for Triage). Returns how many were
    linked."""
    rows = _linked_folders(conn, root_id)
    if not rows or not resource_ids:
        return 0
    fresh = set(resource_ids)
    ids = sorted(fresh)
    found: list[tuple[int, str, _Entry]] = []
    for start in range(0, len(ids), BATCH):
        for rid, relpath, kind, size, fp in conn.execute(
            select(
                Resource.id, Resource.relpath, Resource.kind, Resource.size, Resource.fingerprint
            )
            .where(
                Resource.id.in_(ids[start : start + BATCH]),
                Resource.root_id == root_id,
                Resource.parent_resource_id.is_(None),
                Resource.status != ResourceStatus.MISSING,
            )
            .order_by(Resource.relpath)
        ):
            found.append((rid, relpath, _Entry(rid, kind, size, fp)))
    linked = 0
    for rid, relpath, entry in found:
        for other_root, other_path, first_root, first_path in rows:
            for here_root, here, there_root, there, in_other in (
                (other_root, other_path, first_root, first_path, True),
                (first_root, first_path, other_root, other_path, False),
            ):
                if here_root != root_id or not _within(relpath, here):
                    continue
                inside = relpath[len(here) + 1 :] if relpath != here else ""
                counterpart = _at(conn, there_root, f"{there}/{inside}" if inside else there)
                if counterpart is None or not _same(entry, counterpart):
                    continue
                groups = _firsts(conn, [rid, counterpart.id])
                if groups.get(rid, rid) == groups.get(counterpart.id, counterpart.id):
                    continue
                if counterpart.id in fresh:  # both new: the second folder's joins the first's
                    place, first = (rid, counterpart.id) if in_other else (counterpart.id, rid)
                else:  # the one there already has the item
                    place, first = rid, counterpart.id
                try:
                    _link(conn, schema, place, first, "inside")
                except PlaceError as e:
                    logger.info("Not linking %s in %s: %s", relpath, root_id, e)
                    continue
                linked += 1
    return linked


def _at(conn: Connection, root_id: str, relpath: str) -> _Entry | None:
    row = conn.execute(
        select(Resource.id, Resource.kind, Resource.size, Resource.fingerprint).where(
            Resource.root_id == root_id,
            Resource.relpath == relpath,
            Resource.parent_resource_id.is_(None),
            Resource.status != ResourceStatus.MISSING,
        )
    ).first()
    return None if row is None else _Entry(*row)


@dataclass(frozen=True)
class FolderDifference:
    """A file in a linked folder that differs from the one at its path in the folder it
    is linked with, so it isn't linked (Triage)."""

    resource_id: int
    root_id: str
    root_name: str
    relpath: str
    first_id: int
    first_root_id: str
    first_relpath: str
    first_shown: str
    """Where the one it differs from is: its root's name and path."""


def folder_differences(conn: Connection) -> list[FolderDifference]:
    """Every linked folder's files that differ from their counterparts, by folder."""
    names = _root_names(conn)
    found: list[FolderDifference] = []
    for root_id, relpath, first_root, first_path in _linked_folders(conn):
        other = Folder(root_id, names.get(root_id, root_id), relpath)
        first = Folder(first_root, names.get(first_root, first_root), first_path)
        for paired in folder_pair(conn, first, other).differ:
            inside = f"/{paired.inside}" if paired.inside else ""
            found.append(
                FolderDifference(
                    paired.other_id,
                    root_id,
                    other.root_name,
                    relpath + inside,
                    paired.first_id,
                    first_root,
                    first_path + inside,
                    first.shown + inside,
                )
            )
    return found


def count_folder_differences(conn: Connection) -> int:
    return len(folder_differences(conn))
