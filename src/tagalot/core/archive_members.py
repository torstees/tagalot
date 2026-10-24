"""Archives' members as resources (#391, DESIGN.md §6 *Archive members*).

Files inside an archive the user chose to look inside are recorded as resources of their
own: ``parent_resource_id`` is the archive, and ``relpath`` the archive's path and the
member's path inside it (``Aurora Studio/pack.zip/fonts/Regular.ttf``), which can't clash
with a real file, as the archive is a file, not a folder. The theme gets them like files,
counting in the archive's folder, and reads an extracted temporary copy; nothing is ever
written into an archive.

Which members are read:

- an archive the user chose **all** for, or under one of its root's ``archives`` patterns
  (and not chosen **none**): every member, those added later too;
- one chosen **member by member**: those marked read in ``archive_member_choice``.
  Members that appear later are recorded as undecided (``read`` ``None``) and not read,
  for Triage to list (#520).

A scan lists an archive's members only when it changed since it was last listed (its size
or modification time), or was never listed. Members the archive no longer holds are
``missing``; members no longer wanted are ``skipped``, keeping their items and tags, as a
Skip pattern does (#152). An archive that is offline or missing makes its members so too,
and one that is skipped, or no longer read, makes them skipped; its listing is then read
again when it is back.

Members the theme doesn't take (by extension), folders, archives inside the archive, and
hidden files (``__MACOSX/``, names starting with ``.``) are left out.
"""

import logging
import re
import tempfile
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath

import rarfile
from sqlalchemy import Connection, delete, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core.fingerprint import fingerprint_file
from tagalot.core.member_paths import member_name, member_relpath
from tagalot.core.models import (
    ArchiveMemberChoice,
    ArchiveMode,
    ArchiveRead,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from tagalot.core.scanner import compile_excludes
from tagalot.core.see_through import folder_of
from tagalot.core.thumbnails.archive import ArchiveError, ArchiveMember, open_archive
from tagalot.themes.api import KIND_EXTENSIONS, Kind, ResourceInfo

logger = logging.getLogger(__name__)

ARCHIVE_EXTENSIONS = KIND_EXTENSIONS[Kind.ARCHIVE]
MAX_MEMBER_BYTES = 256 * 1024 * 1024
"""Members larger than this aren't read: the theme would get them through memory."""
BATCH = 500


@dataclass(frozen=True)
class MemberEntry:
    """A file inside an archive, as listed."""

    name: str
    """Its path inside the archive, with ``/``."""
    ext: str
    size: int
    mtime_ns: int | None


def list_members(path: str, extensions: Collection[str] | None = None) -> list[MemberEntry]:
    """The files inside the archive at ``path`` that can be resources: of ``extensions``
    (``None``: any), not folders, archives, or hidden files. Raises :class:`ArchiveError`
    or ``OSError`` when the archive can't be read."""
    try:
        with open_archive(path) as archive:
            members = archive.members()
    except rarfile.Error as e:  # no tool to read it, or a damaged one
        raise ArchiveError(str(e) or type(e).__name__) from e
    found = []
    for member in members:
        entry = _entry(member, extensions)
        if entry is not None:
            found.append(entry)
    return found


def _entry(member: ArchiveMember, extensions: Collection[str] | None) -> MemberEntry | None:
    if member.is_dir or member.encrypted:
        return None
    parts = [p for p in member.name.split("/") if p]
    if not parts or any(p.startswith(".") or p == "__MACOSX" for p in parts):
        return None
    ext = PurePosixPath(parts[-1]).suffix.lower()
    if ext in ARCHIVE_EXTENSIONS or (extensions is not None and ext not in extensions):
        return None
    mtime = int(member.modified.timestamp() * 1_000_000_000) if member.modified else None
    return MemberEntry("/".join(parts), ext, member.size, mtime)


# --- which archives are read ---


@dataclass(frozen=True)
class ArchiveState:
    """An archive resource of a root, with what is known of reading it."""

    id: int
    relpath: str
    size: int | None
    mtime_ns: int | None
    status: ResourceStatus
    skipped: bool
    mode: ArchiveMode | None
    """The user's choice for it, if any."""
    listed: tuple[int | None, int | None] | None
    """``(size, mtime_ns)`` when its members were last listed, if ever."""
    has_members: bool

    def effective(self, patterns: re.Pattern[str] | None) -> ArchiveMode:
        """What is read inside it: the user's choice, else all under a pattern, else none."""
        if self.mode is not None:
            return self.mode
        if patterns is not None and patterns.match(self.relpath.casefold()):
            return ArchiveMode.ALL
        return ArchiveMode.NONE

    def needs_listing(self) -> bool:
        return self.listed != (self.size, self.mtime_ns)


def archive_states(conn: Connection, root_id: str) -> list[ArchiveState]:
    """The root's archives (files with an archive's extension, not inside another)."""
    rows = conn.execute(
        select(
            Resource.id,
            Resource.relpath,
            Resource.size,
            Resource.mtime_ns,
            Resource.status,
            Resource.skipped,
            ArchiveRead.mode,
            ArchiveRead.listed_size,
            ArchiveRead.listed_mtime_ns,
        )
        .outerjoin(ArchiveRead, ArchiveRead.resource_id == Resource.id)
        .where(
            Resource.root_id == root_id,
            Resource.parent_resource_id.is_(None),
            Resource.kind == ResourceKind.FILE,
            Resource.ext.in_(sorted(ARCHIVE_EXTENSIONS)),
        )
        .order_by(Resource.relpath)
    ).all()
    holding = set(
        conn.scalars(
            select(Resource.parent_resource_id)
            .where(Resource.root_id == root_id, Resource.parent_resource_id.is_not(None))
            .distinct()
        )
    )
    return [
        ArchiveState(
            rid,
            rel,
            size,
            mtime,
            status,
            skipped,
            mode,
            None if listed_size is None and listed_mtime is None else (listed_size, listed_mtime),
            rid in holding,
        )
        for rid, rel, size, mtime, status, skipped, mode, listed_size, listed_mtime in rows
    ]


def archive_patterns(patterns: Iterable[str]) -> re.Pattern[str] | None:
    """A root's ``archives`` patterns, compiled like its Skip patterns."""
    return compile_excludes(patterns)


# --- syncing (DB writer jobs) ---


@dataclass(frozen=True)
class MemberPlan:
    """What a scan found inside one archive it reads."""

    archive: ArchiveState
    mode: ArchiveMode
    listing: Sequence[MemberEntry]


@dataclass
class MemberCounts:
    """What syncing members did, for the scan report."""

    new: int = 0
    changed: int = 0
    missing: int = 0
    skipped: int = 0
    undecided: int = 0


def set_aside(conn: Connection, archive: ArchiveState) -> None:
    """An archive whose members aren't read now (offline, missing, skipped, or not chosen):
    its members follow it, keeping their items, and its listing is read again when it is
    back."""
    members = Resource.parent_resource_id == archive.id
    if archive.status is not ResourceStatus.OK:
        conn.execute(update(Resource).where(members).values(status=archive.status))
    else:
        conn.execute(update(Resource).where(members).values(skipped=True))
    conn.execute(
        update(ArchiveRead)
        .where(ArchiveRead.resource_id == archive.id)
        .values(listed_size=None, listed_mtime_ns=None)
    )


def wake_members(conn: Connection, archive_id: int) -> None:
    """An archive that is read and hasn't changed since it was listed: members it took
    offline with it (it, or its root, was unreachable) are back."""
    conn.execute(
        update(Resource)
        .where(
            Resource.parent_resource_id == archive_id,
            Resource.status == ResourceStatus.OFFLINE,
        )
        .values(status=ResourceStatus.OK)
    )


def apply_listing(conn: Connection, root_id: str, plan: MemberPlan, when: datetime) -> MemberCounts:
    """Record an archive's members from its listing (see the module's docstring)."""
    counts = MemberCounts()
    archive = plan.archive
    listing = plan.listing
    known = {
        row.relpath: row
        for row in conn.execute(
            select(
                Resource.id,
                Resource.relpath,
                Resource.size,
                Resource.mtime_ns,
                Resource.status,
                Resource.skipped,
            ).where(Resource.parent_resource_id == archive.id)
        )
    }
    wanted = _wanted(conn, archive.id, plan.mode, listing, counts)
    seen: set[str] = set()
    new_rows = []
    for entry in listing:
        relpath = member_relpath(archive.relpath, entry.name)
        seen.add(relpath)
        row = known.get(relpath)
        want = entry.name in wanted
        if row is None:
            if want:
                new_rows.append(
                    {
                        "root_id": root_id,
                        "relpath": relpath,
                        "kind": ResourceKind.FILE,
                        "ext": entry.ext,
                        "size": entry.size,
                        "mtime_ns": entry.mtime_ns,
                        "status": ResourceStatus.OK,
                        "parent_resource_id": archive.id,
                        "first_seen_at": when,
                        "last_seen_at": when,
                    }
                )
            continue
        values: dict[str, object] = {"last_seen_at": when, "status": ResourceStatus.OK}
        if (row.size, row.mtime_ns) != (entry.size, entry.mtime_ns):
            values |= {
                "size": entry.size,
                "mtime_ns": entry.mtime_ns,
                "fingerprint": None,
                "ingested_at": None,
            }
            counts.changed += 1
        if row.skipped == want:  # wanted and skipped, or not wanted and shown
            values["skipped"] = not want
            if not want:
                counts.skipped += 1
        conn.execute(update(Resource).where(Resource.id == row.id).values(**values))
    for start in range(0, len(new_rows), BATCH):
        conn.execute(insert(Resource), new_rows[start : start + BATCH])
    counts.new = len(new_rows)
    gone = [row.id for rel, row in known.items() if rel not in seen]
    for start in range(0, len(gone), BATCH):
        chunk = gone[start : start + BATCH]
        conn.execute(
            update(Resource).where(Resource.id.in_(chunk)).values(status=ResourceStatus.MISSING)
        )
    counts.missing = len(gone)
    _stamp(conn, archive)
    return counts


def _wanted(
    conn: Connection,
    archive_id: int,
    mode: ArchiveMode,
    listing: Sequence[MemberEntry],
    counts: MemberCounts,
) -> set[str]:
    """The members to read; for a member-by-member archive, recording the new ones as
    undecided and forgetting undecided ones it no longer holds."""
    names = {e.name for e in listing}
    if mode is ArchiveMode.ALL:
        return names
    if mode is ArchiveMode.NONE:
        return set()
    choices = member_choices(conn, archive_id)
    undecided = sorted(names - choices.keys())
    for start in range(0, len(undecided), BATCH):
        conn.execute(
            insert(ArchiveMemberChoice),
            [
                {"archive_id": archive_id, "path": p, "read": None}
                for p in undecided[start : start + BATCH]
            ],
        )
    stale = [p for p, read in choices.items() if read is None and p not in names]
    for start in range(0, len(stale), BATCH):
        conn.execute(
            delete(ArchiveMemberChoice).where(
                ArchiveMemberChoice.archive_id == archive_id,
                ArchiveMemberChoice.path.in_(stale[start : start + BATCH]),
            )
        )
    counts.undecided = sum(1 for p in names if choices.get(p) is None)
    return {p for p in names if choices.get(p)}


def _stamp(conn: Connection, archive: ArchiveState) -> None:
    """Remember the archive as listed at its size and modification time."""
    statement = sqlite_insert(ArchiveRead).values(
        resource_id=archive.id, listed_size=archive.size, listed_mtime_ns=archive.mtime_ns
    )
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=[ArchiveRead.resource_id],
            set_={"listed_size": archive.size, "listed_mtime_ns": archive.mtime_ns},
        )
    )


# --- the user's choices ---


def set_archive_mode(conn: Connection, archive_id: int, mode: ArchiveMode | None) -> None:
    """Choose what to read inside one archive (``None``: as the root's patterns say). Its
    members change at the next scan of its root, which lists it again."""
    statement = sqlite_insert(ArchiveRead).values(
        resource_id=archive_id, mode=mode, listed_size=None, listed_mtime_ns=None
    )
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=[ArchiveRead.resource_id],
            set_={"mode": mode, "listed_size": None, "listed_mtime_ns": None},
        )
    )


def set_member_choices(conn: Connection, archive_id: int, choices: Mapping[str, bool]) -> None:
    """Choose, member by member, what to read inside one archive (paths inside it: read
    or hidden), making it a member-by-member archive. Other members keep their choices."""
    set_archive_mode(conn, archive_id, ArchiveMode.CHOSEN)
    for path, read in choices.items():
        statement = sqlite_insert(ArchiveMemberChoice).values(
            archive_id=archive_id, path=path, read=read
        )
        conn.execute(
            statement.on_conflict_do_update(
                index_elements=[ArchiveMemberChoice.archive_id, ArchiveMemberChoice.path],
                set_={"read": read},
            )
        )


def member_choices(conn: Connection, archive_id: int) -> dict[str, bool | None]:
    """The choices recorded for an archive's members, by path inside it."""
    rows = conn.execute(
        select(ArchiveMemberChoice.path, ArchiveMemberChoice.read).where(
            ArchiveMemberChoice.archive_id == archive_id
        )
    )
    return {path: read for path, read in rows}


# --- reading members for the theme ---


MemberRow = tuple[int, str, ResourceKind, str, int | None, int | None]
"""A member resource's id, relpath, kind, ext, size, and mtime_ns."""


def member_info(
    root_id: str,
    row: MemberRow,
    archive: str,
    archive_path: str,
    see_through: frozenset[str],
) -> ResourceInfo:
    """A member as the theme gets it, before it is extracted (``path`` empty): counting
    in the folder the archive counts in."""
    rid, rel, kind, ext, size, mtime = row
    return ResourceInfo(
        rid,
        root_id,
        rel,
        kind.value,
        ext,
        size,
        mtime,
        "",
        folder_of(archive, see_through),
        see_through,
        archive=archive,
        archive_path=archive_path,
    )


@dataclass(frozen=True)
class Extracted:
    """Members copied out of their archives for the theme to read."""

    resources: list[ResourceInfo]
    """The batch, members' ``path`` now their copies; members that couldn't be read are
    left out."""
    fingerprints: dict[int, bytes]
    errors: list[tuple[str, str]]


@contextmanager
def extracted(batch: list[ResourceInfo]) -> Iterator[Extracted]:
    """Copy the batch's members out of their archives into a temporary folder (each
    archive opened once), for as long as the block runs; the archives are only read. A
    member that can't be read is reported and left out, so it stays pending."""
    members = [r for r in batch if r.archive_path]
    if not members:
        yield Extracted(batch, {}, [])
        return
    with tempfile.TemporaryDirectory(prefix="tagalot-members-") as folder:
        copies: dict[int, str] = {}
        fingerprints: dict[int, bytes] = {}
        errors: list[tuple[str, str]] = []
        by_archive: dict[str, list[ResourceInfo]] = {}
        for member in members:
            assert member.archive_path is not None
            by_archive.setdefault(member.archive_path, []).append(member)
        for number, (path, wanted) in enumerate(by_archive.items()):
            target = Path(folder) / str(number)
            _extract(path, wanted, target, copies, fingerprints, errors)
        kept = [
            replace(r, path=copies[r.id]) if r.archive_path else r
            for r in batch
            if not r.archive_path or r.id in copies
        ]
        yield Extracted(kept, fingerprints, errors)


def _extract(
    path: str,
    wanted: list[ResourceInfo],
    target: Path,
    copies: dict[int, str],
    fingerprints: dict[int, bytes],
    errors: list[tuple[str, str]],
) -> None:
    try:
        with open_archive(path) as archive:
            known = {m.name.strip("/"): m for m in archive.members()}
            for member in wanted:
                assert member.archive is not None
                name = member_name(member.archive, member.relpath)
                found = known.get(name)
                if found is None:
                    errors.append((member.relpath, "It isn't in the archive any more."))
                    continue
                if found.size > MAX_MEMBER_BYTES:
                    errors.append((member.relpath, "It's too large to read inside an archive."))
                    continue
                try:
                    data = archive.read(found, MAX_MEMBER_BYTES)
                except ArchiveError as e:
                    errors.append((member.relpath, str(e)))
                    continue
                copy = target.joinpath(*_safe_parts(name))
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(data)
                copies[member.id] = str(copy)
                fingerprints[member.id] = fingerprint_file(str(copy))
    except (ArchiveError, OSError, rarfile.Error) as e:
        logger.warning("Can't read inside %s: %s", path, e)
        for member in wanted:
            if member.id not in copies:
                errors.append((member.relpath, f"Can't read its archive: {e}"))


_UNSAFE = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def _safe_parts(name: str) -> list[str]:
    """The member's path as parts that are safe file names on any OS."""
    # Split on "/" (not PurePosixPath, whose parts keep a leading "/", which would make
    # the copy's path absolute: outside the temporary folder).
    parts = [_UNSAFE.sub("_", p) for p in name.split("/") if p not in ("", ".", "..")]
    return parts or ["member"]


def store_fingerprints(conn: Connection, fingerprints: Mapping[int, bytes]) -> None:
    """Record members' fingerprints, made from their extracted copies."""
    for resource_id, fingerprint in fingerprints.items():
        conn.execute(
            update(Resource).where(Resource.id == resource_id).values(fingerprint=fingerprint)
        )
