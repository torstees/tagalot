"""Broken links (DESIGN.md §13 *Linked places*, #604).

A symbolic link, junction, or shortcut whose target is gone (deleted, renamed, moved, or
on a drive that isn't connected) is left out of the walk and reported; each scan of a
root records what it found (:func:`record_broken_links`), replacing what the last one
found, so a link whose target is back drops off. Triage's **Links** tab lists them
(:func:`broken_links`), with where they pointed, to be fixed on disk.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, delete, func, insert, select

from tagalot.core.models import BrokenLink, Root


def record_broken_links(
    conn: Connection, root_id: str, found: Sequence[tuple[str, str, str]]
) -> None:
    """Make a root's broken links ``found``: (relative path, how, where it pointed)."""
    conn.execute(delete(BrokenLink).where(BrokenLink.root_id == root_id))
    rows = {relpath: (how, target) for relpath, how, target in found}
    if rows:
        conn.execute(
            insert(BrokenLink),
            [
                {"root_id": root_id, "relpath": relpath, "how": how, "target": target}
                for relpath, (how, target) in sorted(rows.items())
            ],
        )


@dataclass(frozen=True)
class Broken:
    """A broken link, as Triage lists it."""

    root_id: str
    root_name: str
    relpath: str
    how: str
    target: str
    """Where it pointed (a shortcut's paths, one per line)."""

    @property
    def pointed(self) -> str:
        """Where it pointed, on one line."""
        return " or ".join(p for p in self.target.splitlines() if p) or "somewhere unknown"


def broken_links(conn: Connection) -> list[Broken]:
    """Every broken link the last scans found, by folder and path."""
    rows = conn.execute(
        select(BrokenLink.root_id, Root.name, BrokenLink.relpath, BrokenLink.how, BrokenLink.target)
        .join(Root, Root.id == BrokenLink.root_id)
        .order_by(Root.name, BrokenLink.relpath)
    )
    return [Broken(*row) for row in rows]


def count_broken_links(conn: Connection) -> int:
    return int(conn.scalar(select(func.count()).select_from(BrokenLink)) or 0)
