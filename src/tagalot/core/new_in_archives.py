"""Triage's **New in archives** (#520, DESIGN.md §12 *Triage*): archives read member by
member that hold members the user hasn't decided on.

An archive read member by member (``archive_read.mode`` ``chosen``) reads only the members
chosen. When it changes, members it didn't hold before are recorded as undecided
(``archive_member_choice.read`` ``None``) and not read (``core.archive_members``). They're
listed here, by archive, for review: the Choose what to read inside… dialog marks them new
and starts them hidden, and **Hide all new** records them hidden without opening it.

Archives read whole (``all``, or under a root's ``archives`` patterns) aren't listed: their
new members are simply read. Nor are archives read for nothing (``none``), skipped, or
missing.
"""

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import ColumnElement, Connection, func, select, update

from tagalot.core.models import (
    ArchiveMemberChoice,
    ArchiveMode,
    ArchiveRead,
    Resource,
    ResourceStatus,
)

MAX_KINDS = 4
"""Kinds named in an archive's summary; more are counted ("and 2 more")."""


@dataclass(frozen=True)
class NewInArchive:
    """One archive with members not decided on."""

    archive_id: int
    root_id: str
    relpath: str
    """The archive's path in its root."""
    paths: tuple[str, ...]
    """The new members' paths inside the archive."""
    mtime_ns: int | None
    """When the archive last changed."""

    @property
    def name(self) -> str:
        return self.relpath.rpartition("/")[2]

    @property
    def count(self) -> int:
        return len(self.paths)

    @property
    def summary(self) -> str:
        """How many, of which kinds: ``3 new: .ttf, .otf``."""
        kinds = sorted({_kind(p) for p in self.paths})
        named = ", ".join(kinds[:MAX_KINDS])
        more = len(kinds) - MAX_KINDS
        return f"{self.count:,} new: {named}" + (f" and {more} more" if more > 0 else "")


def _kind(path: str) -> str:
    base = path.rpartition("/")[2]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else "no extension"


def _listed() -> list[ColumnElement[bool]]:
    """Where an archive's undecided members are listed: read member by member, and the
    archive itself read and present."""
    return [
        ArchiveMemberChoice.read.is_(None),
        ArchiveRead.mode == ArchiveMode.CHOSEN,
        Resource.skipped.is_(False),
        Resource.status != ResourceStatus.MISSING,
    ]


def new_in_archives(conn: Connection) -> list[NewInArchive]:
    """The archives with members not decided on, the most recently changed first."""
    rows = conn.execute(
        select(
            Resource.id,
            Resource.root_id,
            Resource.relpath,
            Resource.mtime_ns,
            ArchiveMemberChoice.path,
        )
        .join(ArchiveMemberChoice, ArchiveMemberChoice.archive_id == Resource.id)
        .join(ArchiveRead, ArchiveRead.resource_id == Resource.id)
        .where(*_listed())
    )
    archives: dict[int, tuple[str, str, int | None]] = {}
    paths: dict[int, list[str]] = defaultdict(list)
    for archive_id, root_id, relpath, mtime_ns, path in rows:
        archives[archive_id] = (root_id, relpath, mtime_ns)
        paths[archive_id].append(path)
    found = [
        NewInArchive(a, root_id, relpath, tuple(sorted(paths[a])), mtime_ns)
        for a, (root_id, relpath, mtime_ns) in archives.items()
    ]
    return sorted(found, key=lambda n: (-(n.mtime_ns or 0), n.relpath.casefold()))


def count_new_in_archives(conn: Connection) -> int:
    """How many archives have members not decided on."""
    statement = (
        select(func.count(func.distinct(Resource.id)))
        .join(ArchiveMemberChoice, ArchiveMemberChoice.archive_id == Resource.id)
        .join(ArchiveRead, ArchiveRead.resource_id == Resource.id)
        .where(*_listed())
    )
    return int(conn.scalar(statement) or 0)


def hide_new(conn: Connection, archive_ids: Collection[int]) -> int:
    """**Hide all new**: record these archives' undecided members as hidden, so they leave
    the list. They're skipped already (undecided members aren't read). Returns how many."""
    ids = list(archive_ids)
    if not ids:
        return 0
    result = conn.execute(
        update(ArchiveMemberChoice)
        .where(ArchiveMemberChoice.archive_id.in_(ids), ArchiveMemberChoice.read.is_(None))
        .values(read=False)
    )
    return int(result.rowcount or 0)
