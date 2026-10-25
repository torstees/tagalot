"""The user's choices of what to read inside an archive (#519, DESIGN.md §6 *Archive
members*): the dialog's choices, Read everything inside, Stop reading inside, hiding one
member, and reading the archives in a folder.

Each takes effect at once where it can: members no longer read are marked skipped now, so
their items hide (their tags are kept, #526), and the archive is listed again at the next
scan of its root, which reads the members now wanted. The archive itself is only ever read.

An archive read **whole** (``all``, or under one of its root's ``archives`` patterns) stays
whole when members are hidden in it (decided in discussion, 2026-10-24): the hidden ones are
left out (``archive_member_choice.read`` false) and members added later are still read. One
read **member by member** (``chosen``) reads only those marked read; members added later
wait, undecided, for Triage (#520).
"""

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core.archive_members import (
    ARCHIVE_EXTENSIONS,
    archive_patterns,
    member_choices,
    set_archive_mode,
)
from tagalot.core.keep import KeepConfig, RootConfig
from tagalot.core.member_paths import member_relpath
from tagalot.core.models import (
    ArchiveMemberChoice,
    ArchiveMode,
    ArchiveRead,
    Resource,
    ResourceKind,
)
from tagalot.core.root_admin import edit_root
from tagalot.core.triage import exact_pattern


@dataclass(frozen=True)
class ArchiveChoices:
    """What is read inside one archive now, for the dialog and the commands."""

    archive_id: int
    root_id: str
    relpath: str
    mode: ArchiveMode
    """What is read inside it: its own choice, else its root's patterns' (``all`` or
    ``none``)."""
    own: ArchiveMode | None
    """The choice made for this archive, if any (``None``: its root's patterns decide)."""
    choices: dict[str, bool | None]
    """Members' choices by path inside it: read, hidden, or undecided."""

    @property
    def name(self) -> str:
        return self.relpath.rpartition("/")[2]

    def hidden(self) -> set[str]:
        """The members hidden now: chosen hidden; in a member-by-member archive, those not
        chosen to be read too (undecided ones aren't read)."""
        if self.mode is ArchiveMode.CHOSEN:
            return {p for p, read in self.choices.items() if not read}
        if self.mode is ArchiveMode.ALL:
            return {p for p, read in self.choices.items() if read is False}
        return set()  # nothing is read: the dialog starts with everything shown


def is_archive(relpath: str) -> bool:
    """Whether a file (not inside an archive) is one whose inside can be read."""
    name = relpath.rpartition("/")[2]
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in ARCHIVE_EXTENSIONS


def archive_choices(
    conn: Connection, archive_id: int, roots: Sequence[RootConfig]
) -> ArchiveChoices | None:
    """What is read inside the archive ``archive_id`` now, or ``None`` if it isn't an
    archive of a root in ``roots`` (one inside another archive isn't read, #391)."""
    row = conn.execute(
        select(Resource.root_id, Resource.relpath, ArchiveRead.mode)
        .outerjoin(ArchiveRead, ArchiveRead.resource_id == Resource.id)
        .where(
            Resource.id == archive_id,
            Resource.kind == ResourceKind.FILE,
            Resource.parent_resource_id.is_(None),
        )
    ).first()
    if row is None or not is_archive(row.relpath):
        return None
    root = next((r for r in roots if r.id == row.root_id), None)
    if root is None:
        return None
    mode = row.mode
    if mode is None:
        patterns = archive_patterns(root.archives)
        covered = patterns is not None and patterns.match(row.relpath.casefold())
        mode = ArchiveMode.ALL if covered else ArchiveMode.NONE
    return ArchiveChoices(
        archive_id, row.root_id, row.relpath, mode, row.mode, member_choices(conn, archive_id)
    )


def save_choices(
    conn: Connection, current: ArchiveChoices, shown: Collection[str], hidden: Collection[str]
) -> int:
    """The dialog's OK: read ``shown`` and not ``hidden`` (paths inside the archive). An
    archive read whole stays whole, leaving out the hidden ones; any other is read member
    by member from now on. Every member listed is decided: a new one (undecided, which the
    dialog marks new and starts hidden, #520) left hidden is recorded hidden, so the archive
    leaves Triage's New in archives. Hidden members are skipped now. Returns how many are
    hidden."""
    archive_id = current.archive_id
    if current.mode is ArchiveMode.ALL:
        conn.execute(
            delete(ArchiveMemberChoice).where(ArchiveMemberChoice.archive_id == archive_id)
        )
        _choose(conn, archive_id, dict.fromkeys(hidden, False))
        _relist(conn, archive_id)
    else:
        set_archive_mode(conn, archive_id, ArchiveMode.CHOSEN)
        choices: dict[str, bool | None] = dict.fromkeys(shown, True)
        choices.update(dict.fromkeys(hidden, False))
        conn.execute(
            delete(ArchiveMemberChoice).where(ArchiveMemberChoice.archive_id == archive_id)
        )
        _choose(conn, archive_id, choices)
    _skip_members(conn, current, hidden)
    return len(hidden)


def read_everything(conn: Connection, current: ArchiveChoices) -> None:
    """Read every member, those added later too; members hidden before are shown again
    (at the next scan of its root, which lists it again)."""
    set_archive_mode(conn, current.archive_id, ArchiveMode.ALL)
    conn.execute(
        delete(ArchiveMemberChoice).where(ArchiveMemberChoice.archive_id == current.archive_id)
    )


def stop_reading(conn: Connection, current: ArchiveChoices) -> None:
    """Read nothing inside it: its members are skipped now, keeping their items' tags
    for if it is read again. The members' choices are kept, for then."""
    set_archive_mode(conn, current.archive_id, ArchiveMode.NONE)
    conn.execute(
        update(Resource)
        .where(Resource.parent_resource_id == current.archive_id)
        .values(skipped=True)
    )


def hide_members(conn: Connection, current: ArchiveChoices, names: Iterable[str]) -> int:
    """Hide these members (paths inside the archive): left out from now on, and skipped
    now, keeping their tags. The archive keeps its way of reading (whole stays whole).
    Returns how many."""
    wanted = list(dict.fromkeys(names))
    _choose(conn, current.archive_id, dict.fromkeys(wanted, False))
    _skip_members(conn, current, wanted)
    return len(wanted)


def read_folder_archives(
    config: KeepConfig, keep_dir: Path, root_id: str, folder: str
) -> tuple[KeepConfig, str]:
    """``config`` with the archives in ``folder`` (root-relative; ``""``: the whole root)
    read whole: an ``archives`` pattern for it, and the pattern."""
    pattern = f"{exact_pattern(folder)}/**" if folder else "**"
    root = next((r for r in config.roots if r.id == root_id), None)
    if root is None or pattern in root.archives:
        return config, pattern
    return edit_root(config, keep_dir, root_id, archives=[*root.archives, pattern]), pattern


def _choose(conn: Connection, archive_id: int, choices: dict[str, bool | None]) -> None:
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


def _relist(conn: Connection, archive_id: int) -> None:
    """List the archive again at the next scan, keeping its own choice (or none)."""
    conn.execute(
        update(ArchiveRead)
        .where(ArchiveRead.resource_id == archive_id)
        .values(listed_size=None, listed_mtime_ns=None)
    )


def _skip_members(conn: Connection, current: ArchiveChoices, names: Iterable[str]) -> None:
    relpaths = [member_relpath(current.relpath, n) for n in names]
    for start in range(0, len(relpaths), 500):
        conn.execute(
            update(Resource)
            .where(
                Resource.parent_resource_id == current.archive_id,
                Resource.relpath.in_(relpaths[start : start + 500]),
            )
            .values(skipped=True)
        )
