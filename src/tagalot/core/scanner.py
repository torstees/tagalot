"""Walking roots, diffing against the database, and move detection."""

import logging
import os
import posixpath
import re
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, bindparam, delete, exists, insert, select, update

from tagalot.core.models import (
    ArchiveMemberChoice,
    ArchiveRead,
    Entity,
    EntityResource,
    LinkedPlace,
    PlaceContains,
    Resource,
    ResourceKind,
    ResourceStatus,
    Root,
)
from tagalot.core.notes import NOTE_EXTENSIONS, note_rank
from tagalot.core.see_through import logical
from tagalot.themes.api import KIND_EXTENSIONS, Kind

logger = logging.getLogger(__name__)

ErrorHandler = Callable[[str, OSError], None]
"""Called with the relative path of a folder or file that could not be read, and the error."""

DirRule = bool | Callable[[str], bool]
"""Whether to record directories as resources: all, none, or those a predicate accepts."""


@dataclass(frozen=True)
class WalkEntry:
    """A file or directory found under a root, before comparison with the database."""

    relpath: str
    """POSIX-style path relative to the root, e.g. ``Artist/Album/01 Song.flac``."""
    kind: ResourceKind
    ext: str
    """Lowercase extension with the dot (``.flac``), or empty; always empty for directories."""
    size: int | None
    """File size in bytes; ``None`` for directories."""
    mtime_ns: int
    target: str | None = None
    """Reached through a link (#599): the real path this is (a symbolic link's or a
    junction's target, or the path a linked folder's entry really has), else ``None``."""
    how: str | None = None
    """How it was reached through a link: ``symlink``, ``junction``, or ``inside`` (in a
    linked folder)."""
    file_id: tuple[int, int] | None = None
    """``(device, file number)`` of a file with more than one hard link, when the walk
    asked and the disk says (#599)."""


def compile_excludes(patterns: Iterable[str]) -> re.Pattern[str] | None:
    """Compile root exclude globs into one case-insensitive regex over POSIX relative paths.

    ``*`` and ``?`` stay within one path segment; ``**`` spans segments. ``**/x`` matches ``x``
    at any depth, including the top, and ``x/**`` matches the folder ``x`` itself and all of
    its contents, so an excluded folder is pruned rather than walked.
    """
    parts = [_glob_to_regex(p) for p in patterns if p.strip()]
    if not parts:
        return None
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


def _glob_to_regex(pattern: str) -> str:
    pattern = pattern.strip().replace("\\", "/").strip("/")
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and (end := pattern.find("]", i + 2)) != -1:
            body = pattern[i + 1 : end]
            negate = body.startswith("!")
            # Keep ranges; escape what a regex class would read otherwise ([ opens a set).
            chars = body[negate:].replace("\\", "\\\\").replace("^", "\\^").replace("[", "\\[")
            out.append("[" + ("^" if negate else "") + chars + "]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out) + r"\Z"


def walk_root(
    root_path: str,
    *,
    exclude: Iterable[str] = (),
    extensions: Collection[str] | None = None,
    dirs: DirRule = False,
    on_error: ErrorHandler | None = None,
    notes: str | None = None,
    see_through: frozenset[str] = frozenset(),
    link_roots: Sequence[str] | None = None,
    hard_links: bool = False,
) -> Iterator[WalkEntry]:
    """Yield the files (and optionally directories) under ``root_path``.

    - ``exclude``: glob patterns (see :func:`compile_excludes`); matching folders are pruned.
    - ``extensions``: lowercase extensions such as ``{".flac", ".jpg"}``; ``None`` keeps all.
    - ``dirs``: whether directories become entries (the root itself never does).
    - Notes (§9 *Notes*): a Markdown file named as its folder's note is listed whatever
      ``extensions`` say, when its folder is one ``dirs`` lists (:func:`is_folder_note`).
    - ``notes``: the notes folder in this root (§9 *The notes folder*; ``""`` is the whole
      root): only its Markdown files are listed, whatever ``extensions`` and ``dirs`` say.
    - ``see_through``: see-through folder names, case-folded (#459): a note in one is its
      parent folder's.
    - Links (#599): a symbolic link to a file is listed as the file, with its target. A
      symbolic link or junction to a folder inside ``link_roots`` (real paths; the root's
      own by default) is walked, its entries listed with the real paths they have
      (``inside``); one to a folder elsewhere, or to a folder it is inside (a loop), is
      not walked. A link whose target is gone is left out.
    - ``hard_links``: also ask each file how many names it has (a second ``stat`` on
      Windows), so a file with several gets its :attr:`WalkEntry.file_id`.
    - Unreadable folders and files go to ``on_error`` and the walk continues.

    Entries are yielded in name order within each folder, so results are deterministic.
    """
    excluded = compile_excludes(exclude)
    wanted = {e.lower() for e in extensions} if extensions is not None else None
    report = on_error or _log_error
    real_root = os.path.realpath(root_path)
    allowed = [_norm(p) for p in (link_roots if link_roots is not None else [real_root])]
    # Each folder to walk: its real path, whether it is in a linked folder, and the real
    # paths of the folders it is in (a link back to one of them is a loop).
    stack: list[tuple[str, str, bool, tuple[str, ...]]] = [
        ("", real_root, False, (_norm(real_root),))
    ]
    while stack:
        rel_dir, real_dir, linked, chain = stack.pop()
        try:
            with os.scandir(os.path.join(root_path, rel_dir) if rel_dir else root_path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            report(rel_dir, e)
            continue
        subdirs: list[tuple[str, str, bool, tuple[str, ...]]] = []
        for entry in entries:
            relpath = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if excluded is not None and excluded.match(relpath):
                continue
            real = os.path.join(real_dir, entry.name)
            target, how = (real, "inside") if linked else (None, None)
            try:
                if entry.is_symlink() or entry.is_junction():
                    target = os.path.realpath(entry.path)
                    how = "junction" if entry.is_junction() else "symlink"
                    if os.path.isdir(target):
                        if not _may_walk(target, chain, allowed):
                            continue  # outside the keep's folders, or a loop
                        real = target
                    elif not os.path.isfile(target):
                        continue  # its target is gone
                    is_dir = os.path.isdir(target)
                else:
                    is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir:
                    if in_notes(relpath, notes):
                        pass  # walked for its notes, never an entry
                    elif dirs is True or (callable(dirs) and dirs(relpath)):
                        yield WalkEntry(
                            relpath=relpath,
                            kind=ResourceKind.DIR,
                            ext="",
                            size=None,
                            mtime_ns=entry.stat(follow_symlinks=False).st_mtime_ns,
                            target=target,
                            how=how,
                        )
                    subdirs.append((relpath, real, target is not None, (*chain, _norm(real))))
                elif entry.is_file():
                    ext = os.path.splitext(entry.name)[1].lower()
                    if in_notes(relpath, notes):
                        if ext not in NOTES_FOLDER_EXTENSIONS:
                            continue
                    elif (
                        wanted is not None
                        and ext not in wanted
                        and not is_folder_note(relpath, dirs, see_through)
                    ):
                        continue
                    st = entry.stat()
                    yield WalkEntry(
                        relpath=relpath,
                        kind=ResourceKind.FILE,
                        ext=ext,
                        size=st.st_size,
                        mtime_ns=st.st_mtime_ns,
                        target=target,
                        how=how,
                        file_id=_file_id(entry, st) if hard_links and how is None else None,
                    )
            except OSError as e:
                report(relpath, e)
        stack.extend(reversed(subdirs))  # pop in name order: depth-first, sorted


def _norm(path: str) -> str:
    """A real path as compared: case-folded where the OS ignores case."""
    return os.path.normcase(os.path.normpath(path))


def _within(path: str, folder: str) -> bool:
    """Whether a normalized path is ``folder`` or inside it."""
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _may_walk(target: str, chain: Sequence[str], allowed: Sequence[str]) -> bool:
    """Whether a link to the folder ``target`` (a real path) is walked: inside one of the
    ``allowed`` folders, and not one holding a folder on the way to it (``chain``, real
    paths), which would walk it forever."""
    norm = _norm(target)
    if any(_within(folder, norm) for folder in chain):
        return False  # a loop: the link leads back up
    return any(_within(norm, folder) for folder in allowed)


def _file_id(entry: os.DirEntry[str], st: os.stat_result) -> tuple[int, int] | None:
    """A file's ``(device, file number)`` when it has more than one hard link. Windows'
    directory listings leave these out, so they are asked for there."""
    if os.name == "nt":
        st = os.stat(entry.path)
    if st.st_nlink > 1 and st.st_ino:
        return (st.st_dev, st.st_ino)
    return None


NOTES_FOLDER_EXTENSIONS = frozenset(NOTE_EXTENSIONS) | (
    KIND_EXTENSIONS[Kind.IMAGE] | KIND_EXTENSIONS[Kind.ARCHIVE]
)
"""What the notes folder records: its notes, and the pictures and archives of their
galleries (#388). None of it is handed to the theme."""


def in_notes(relpath: str, notes: str | None) -> bool:
    """Whether a root-relative path is the notes folder ``notes`` or inside it (``""``: the
    whole root; ``None``: the root has none). Compared ignoring case, as Windows does."""
    if notes is None:
        return False
    if notes == "":
        return True
    path, folder = relpath.casefold(), notes.casefold()
    return path == folder or path.startswith(folder + "/")


def is_folder_note(relpath: str, dirs: DirRule, see_through: frozenset[str] = frozenset()) -> bool:
    """Whether a file is named as the note of a folder the walk lists (#379): in a
    see-through folder (#459), of the folder it counts as in."""
    relpath = logical(relpath, see_through)
    folder = relpath.rpartition("/")[0]
    if not folder or note_rank(relpath) is None:
        return False
    return dirs is True or (callable(dirs) and bool(dirs(folder)))


def _log_error(relpath: str, error: OSError) -> None:
    logger.warning("Cannot read %s: %s", relpath or "(root)", error)


# --- Diff (DESIGN.md §6 step 3) ---

BATCH_SIZE = 500
"""Rows per statement when applying a diff; keeps each statement well under SQLite limits."""


@dataclass(frozen=True)
class KnownResource:
    """The parts of a stored resource the diff compares."""

    id: int
    kind: ResourceKind
    size: int | None
    mtime_ns: int | None
    status: ResourceStatus
    ext: str = ""
    skipped: bool = False


@dataclass
class RootDiff:
    """What a walk changes for one root."""

    new: list[WalkEntry] = field(default_factory=list)
    changed: list[tuple[int, WalkEntry]] = field(default_factory=list)
    """Existing resources whose kind, size, or mtime changed; fingerprints are cleared."""
    restored: list[int] = field(default_factory=list)
    """Unchanged resources seen again after being offline or missing."""
    missing: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)
    """Seen, unchanged, and already ok: only ``last_seen_at`` moves."""
    skipped: list[int] = field(default_factory=list)
    """Not seen because the scan now leaves them out (excludes, extensions): not missing."""

    @property
    def is_empty(self) -> bool:
        return not (self.new or self.changed or self.restored or self.missing or self.skipped)


def load_known(conn: Connection, root_id: str) -> dict[str, KnownResource]:
    """Read a root's stored resources, keyed by relative path."""
    rows = conn.execute(
        select(
            Resource.relpath,
            Resource.id,
            Resource.kind,
            Resource.size,
            Resource.mtime_ns,
            Resource.status,
            Resource.ext,
            Resource.skipped,
        ).where(Resource.root_id == root_id, Resource.parent_resource_id.is_(None))
    )
    return {
        relpath: KnownResource(id, kind, size, mtime_ns, status, ext, skipped)
        for relpath, id, kind, size, mtime_ns, status, ext, skipped in rows
    }


InScope = Callable[[str, ResourceKind, str], bool]
"""``in_scope(relpath, kind, ext)``: whether a walk with these settings would list it."""


def walk_scope(
    exclude: Iterable[str] = (),
    extensions: Collection[str] | None = None,
    dirs: DirRule = False,
    notes: str | None = None,
    see_through: frozenset[str] = frozenset(),
) -> InScope:
    """Whether :func:`walk_root` with these settings would list a path, if it's there: not
    excluded (itself or a folder above it, since excluded folders are pruned), a wanted
    extension for files, and wanted for folders."""
    excluded = compile_excludes(exclude)
    wanted = {e.lower() for e in extensions} if extensions is not None else None

    def in_scope(relpath: str, kind: ResourceKind, ext: str) -> bool:
        if excluded is not None:
            parts = relpath.split("/")
            if any(excluded.match("/".join(parts[:n])) for n in range(1, len(parts) + 1)):
                return False
        if in_notes(relpath, notes):
            return kind is ResourceKind.FILE and ext.lower() in NOTES_FOLDER_EXTENSIONS
        if kind is ResourceKind.DIR:
            return dirs is True or (callable(dirs) and bool(dirs(relpath)))
        return wanted is None or ext.lower() in wanted or is_folder_note(relpath, dirs, see_through)

    return in_scope


def diff_root(
    known: Mapping[str, KnownResource],
    entries: Iterable[WalkEntry],
    unreadable: Collection[str] = (),
    in_scope: InScope | None = None,
) -> RootDiff:
    """Compare a walk with stored resources.

    ``unreadable`` lists relative paths the walk could not read (from its error callback).
    Stored resources at or under those paths are left as they are rather than marked missing:
    not being able to look is not the same as the file being gone.

    ``in_scope`` (from :func:`walk_scope`) tells which stored resources the walk could have
    listed: those it now leaves out (a new exclude pattern, an extension the theme no longer
    takes) are *skipped*, not missing; the file may well still be there.
    """
    diff = RootDiff()
    seen: set[str] = set()
    for entry in entries:
        seen.add(entry.relpath)
        old = known.get(entry.relpath)
        if old is None:
            diff.new.append(entry)
        elif (old.kind, old.size, old.mtime_ns) != (entry.kind, entry.size, entry.mtime_ns):
            diff.changed.append((old.id, entry))
        elif old.status is not ResourceStatus.OK or old.skipped:
            diff.restored.append(old.id)
        else:
            diff.unchanged.append(old.id)
    blind = tuple(unreadable)
    for relpath, old in known.items():
        if relpath in seen:
            continue
        if in_scope is not None and not in_scope(relpath, old.kind, old.ext):
            if not old.skipped:
                diff.skipped.append(old.id)
            continue
        if old.status is ResourceStatus.MISSING:
            continue
        if any(relpath == u or relpath.startswith(f"{u}/") or not u for u in blind):
            continue
        diff.missing.append(old.id)
    return diff


@dataclass(frozen=True)
class AppliedDiff:
    """The outcome of :func:`apply_diff`, for move detection and ingest."""

    new_ids: dict[str, int]
    """Relative path -> id of each inserted resource."""
    changed_ids: list[int]
    missing_ids: list[int]


def apply_diff(conn: Connection, root_id: str, diff: RootDiff, when: datetime) -> AppliedDiff:
    """Write a diff for a completed scan of ``root_id``. Called by the DB writer.

    New resources are inserted as ``ok``; changed ones get new size/mtime and a cleared
    fingerprint; seen ones become ``ok`` with ``last_seen_at = when``; unseen ones become
    ``missing``. Nothing is deleted. The root's ``last_scan_at`` is set to ``when``.
    """
    new_ids: dict[str, int] = {}
    for batch in _chunks(diff.new):
        rows = conn.execute(
            insert(Resource).returning(Resource.relpath, Resource.id),
            [
                {
                    "root_id": root_id,
                    "relpath": e.relpath,
                    "kind": e.kind,
                    "ext": e.ext,
                    "size": e.size,
                    "mtime_ns": e.mtime_ns,
                    "status": ResourceStatus.OK,
                    "first_seen_at": when,
                    "last_seen_at": when,
                }
                for e in batch
            ],
        )
        new_ids.update({relpath: id for relpath, id in rows})

    for changed in _chunks(diff.changed):
        conn.execute(
            update(Resource)
            .where(Resource.id == bindparam("rid"))
            .values(
                kind=bindparam("kind"),
                ext=bindparam("ext"),
                size=bindparam("size"),
                mtime_ns=bindparam("mtime_ns"),
                fingerprint=None,
                ingested_at=None,
            ),
            [
                {"rid": rid, "kind": e.kind, "ext": e.ext, "size": e.size, "mtime_ns": e.mtime_ns}
                for rid, e in changed
            ],
        )
    seen_ids = [rid for rid, _ in diff.changed] + diff.restored + diff.unchanged
    for ids in _chunks(seen_ids):
        conn.execute(
            update(Resource)
            .where(Resource.id.in_(ids))
            .values(status=ResourceStatus.OK, last_seen_at=when, skipped=False)
        )
    for ids in _chunks(diff.skipped):
        conn.execute(update(Resource).where(Resource.id.in_(ids)).values(skipped=True))
    for ids in _chunks(diff.missing):
        conn.execute(
            update(Resource).where(Resource.id.in_(ids)).values(status=ResourceStatus.MISSING)
        )
    conn.execute(update(Root).where(Root.id == root_id).values(last_scan_at=when))
    return AppliedDiff(
        new_ids=new_ids,
        changed_ids=[rid for rid, _ in diff.changed],
        missing_ids=list(diff.missing),
    )


def split_diff(diff: RootDiff, size: int = BATCH_SIZE) -> list[RootDiff]:
    """Split a diff into parts of at most ``size`` changes each.

    Each part can be applied in its own short transaction: the parts are independent, and
    re-scanning after an interruption finishes the job, because the diff is recomputed.
    """
    parts: list[RootDiff] = []
    for name in ("new", "changed", "restored", "missing", "unchanged", "skipped"):
        items: list[Any] = getattr(diff, name)
        for chunk in _chunks(items, size):
            parts.append(RootDiff(**{name: list(chunk)}))
    return parts or [RootDiff()]


def merge_applied(parts: Iterable[AppliedDiff]) -> AppliedDiff:
    """Combine the results of applying the parts of a split diff."""
    new_ids: dict[str, int] = {}
    changed: list[int] = []
    missing: list[int] = []
    for part in parts:
        new_ids.update(part.new_ids)
        changed += part.changed_ids
        missing += part.missing_ids
    return AppliedDiff(new_ids=new_ids, changed_ids=changed, missing_ids=missing)


def _chunks[T](items: Sequence[T], size: int = BATCH_SIZE) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# --- Move detection (DESIGN.md §6 step 4) ---


@dataclass(frozen=True)
class Move:
    """A missing resource recognized at a new path; its links now belong to ``new_id``."""

    old_id: int
    new_id: int
    old_path: str
    new_path: str


@dataclass(frozen=True)
class _Candidate:
    id: int
    path: str
    """``root_id:relpath``, for messages and name matching."""


def detect_moves(conn: Connection, candidate_ids: Collection[int] | None = None) -> list[Move]:
    """Reattach missing resources to new paths with the same fingerprint. Called by the writer.

    A *new* resource is an ``ok`` file with a fingerprint and no entity links (optionally
    limited to ``candidate_ids``); a linked one is a copy, left for dedupe. It pairs with a
    ``missing`` resource with the same fingerprint, in any root. When several share a
    fingerprint, pairs are made only where the file name decides uniquely; the rest are left
    alone. For each pair, role links, thumbnails, archive members, and ``first_seen_at`` move
    to the new resource, and the stale row is deleted.
    """
    linked = exists().where(EntityResource.resource_id == Resource.id)
    new_query = select(Resource.id, Resource.root_id, Resource.relpath, Resource.fingerprint).where(
        Resource.status == ResourceStatus.OK,
        Resource.kind == ResourceKind.FILE,
        Resource.fingerprint.is_not(None),
        ~linked,
        Resource.id.not_in(select(LinkedPlace.resource_id)),  # another place of a file (#598)
    )
    if candidate_ids is not None:
        if not candidate_ids:
            return []
        new_query = new_query.where(Resource.id.in_(list(candidate_ids)))
    new_by_fp: dict[bytes, list[_Candidate]] = defaultdict(list)
    for rid, root_id, relpath, fp in conn.execute(new_query):
        if fp is not None:  # guaranteed by the query; narrows the type
            new_by_fp[fp].append(_Candidate(rid, f"{root_id}:{relpath}"))
    if not new_by_fp:
        return []

    missing_by_fp: dict[bytes, list[_Candidate]] = defaultdict(list)
    fps = list(new_by_fp)
    for batch in _chunks(fps):
        rows = conn.execute(
            select(Resource.id, Resource.root_id, Resource.relpath, Resource.fingerprint).where(
                Resource.status == ResourceStatus.MISSING, Resource.fingerprint.in_(batch)
            )
        )
        for rid, root_id, relpath, fp in rows:
            if fp is not None:
                missing_by_fp[fp].append(_Candidate(rid, f"{root_id}:{relpath}"))

    moves: list[Move] = []
    for fp, olds in missing_by_fp.items():
        for old, new in _pair(olds, new_by_fp[fp]):
            _transfer(conn, old.id, new.id)
            moves.append(Move(old.id, new.id, old.path, new.path))
            logger.info("Moved: %s -> %s", old.path, new.path)
    return moves


def _pair(olds: list[_Candidate], news: list[_Candidate]) -> list[tuple[_Candidate, _Candidate]]:
    """Pair missing and new resources with one fingerprint, without guessing."""
    if len(olds) == 1 and len(news) == 1:
        return [(olds[0], news[0])]
    old_names: dict[str, list[_Candidate]] = defaultdict(list)
    new_names: dict[str, list[_Candidate]] = defaultdict(list)
    for c in olds:
        old_names[_file_name(c)].append(c)
    for c in news:
        new_names[_file_name(c)].append(c)
    pairs = [
        (old_names[n][0], new_names[n][0])
        for n in old_names
        if len(old_names[n]) == 1 and len(new_names.get(n, [])) == 1
    ]
    if len(pairs) < max(len(olds), len(news)):
        logger.info(
            "Ambiguous move: %d missing and %d new files share a fingerprint; paired %d by name",
            len(olds),
            len(news),
            len(pairs),
        )
    return pairs


def _file_name(c: _Candidate) -> str:
    return posixpath.basename(c.path).casefold()


def _move_members(conn: Connection, old_id: int, new_id: int) -> None:
    """An archive moved: the files inside it read as resources (#391) follow it, their
    paths now under its new one, and so do the choices made for it."""
    old_path = conn.scalar(select(Resource.relpath).where(Resource.id == old_id))
    new = conn.execute(select(Resource.root_id, Resource.relpath).where(Resource.id == new_id))
    found = new.one_or_none()
    if old_path is None or found is None:
        return
    root_id, new_path = found
    members = conn.execute(
        select(Resource.id, Resource.relpath).where(Resource.parent_resource_id == old_id)
    ).all()
    for member_id, relpath in members:
        inside = relpath[len(old_path) + 1 :]
        conn.execute(
            update(Resource)
            .where(Resource.id == member_id)
            .values(parent_resource_id=new_id, root_id=root_id, relpath=f"{new_path}/{inside}")
        )
    if (
        conn.scalar(select(ArchiveRead.resource_id).where(ArchiveRead.resource_id == new_id))
        is None
    ):
        conn.execute(
            update(ArchiveRead).where(ArchiveRead.resource_id == old_id).values(resource_id=new_id)
        )
    conn.execute(
        update(ArchiveMemberChoice)
        .where(ArchiveMemberChoice.archive_id == old_id)
        .values(archive_id=new_id)
        .prefix_with("OR IGNORE")
    )


def _move_places(conn: Connection, old_id: int, new_id: int) -> None:
    """A linked place, or a first place, moved (#598): its links, and the containers it
    files its item in, follow it."""
    conn.execute(
        update(LinkedPlace)
        .where(LinkedPlace.resource_id == old_id)
        .values(resource_id=new_id)
        .prefix_with("OR IGNORE")
    )
    conn.execute(update(LinkedPlace).where(LinkedPlace.first_id == old_id).values(first_id=new_id))
    conn.execute(delete(LinkedPlace).where(LinkedPlace.resource_id == LinkedPlace.first_id))
    conn.execute(
        update(PlaceContains)
        .where(PlaceContains.resource_id == old_id)
        .values(resource_id=new_id)
        .prefix_with("OR IGNORE")
    )


def _transfer(conn: Connection, old_id: int, new_id: int) -> None:
    conn.execute(
        update(EntityResource)
        .where(EntityResource.resource_id == old_id)
        .values(resource_id=new_id)
        .prefix_with("OR IGNORE")  # a link that already exists on the new row wins
    )
    conn.execute(
        update(Entity).where(Entity.thumb_resource_id == old_id).values(thumb_resource_id=new_id)
    )
    _move_members(conn, old_id, new_id)
    _move_places(conn, old_id, new_id)
    first_seen = select(Resource.first_seen_at).where(Resource.id == old_id).scalar_subquery()
    conn.execute(update(Resource).where(Resource.id == new_id).values(first_seen_at=first_seen))
    conn.execute(delete(Resource).where(Resource.id == old_id))  # leftover links cascade
