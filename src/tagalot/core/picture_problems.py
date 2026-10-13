"""Triage's Pictures list (DESIGN.md §12 "Triage", #426): items' pictures that weren't found
or can't be drawn.

- **Not found:** a file an item's file names that wasn't found (a Markdown book's
  ``cover``), as the theme reported it (``IngestContext.not_found``, the ``file_not_found``
  table): what the file says, and each path looked in, in order. Each path's state is
  worked out when the list is made, not when it was scanned, so it says what is there now:
  not there, missing since the last scan, skipped, offline, on disk but not scanned, or
  here now (Re-read from file links it). A report stops being listed once the item has a
  file in that role, and is replaced whenever its file is read again.
- **Can't be drawn:** a picture an item has (in any role) that the thumbnail maker failed
  to read (``picture_failure``): corrupt or unsupported, with the error. Not listed once
  the file changes; removed when it is drawn.

Making the list stats the tried paths when nothing is known of them: call it from a worker.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import ColumnElement, Connection, Select, exists, func, select
from sqlalchemy.orm import aliased

from tagalot.core.models import (
    Entity,
    EntityResource,
    FileNotFound,
    PictureFailure,
    Resource,
    ResourceStatus,
)
from tagalot.core.roots import local_path

NOT_FOUND: Literal["not found"] = "not found"
CANT_DRAW: Literal["can't draw"] = "can't draw"

HERE = "Here now: Re-read from file to use it."
NOT_THERE = "Not there."
MISSING_NOW = "Was here, but has been missing since a scan."
SKIPPED = "Here, but its folder's settings skip it in scans."
OFFLINE = "Its watched folder is offline."
UNREACHABLE = "Its watched folder can't be reached on this computer."
NOT_SCANNED = "On disk, but not scanned: scan again, or the theme doesn't read this kind of file."


@dataclass(frozen=True)
class TriedPath:
    """One path looked in, and what is there now."""

    relpath: str
    state: str
    found: bool = False
    """A file the item could use is there now."""


@dataclass(frozen=True)
class PictureProblem:
    """One row of the Pictures list."""

    kind: Literal["not found", "can't draw"]
    entity_id: int
    title: str
    type: str
    role: str
    resource_id: int
    """The file that names the picture (not found), or the picture (can't draw)."""
    root_id: str
    relpath: str
    written: str | None = None
    """What the file says, as written (not found)."""
    tried: tuple[TriedPath, ...] = ()
    reason: str | None = None
    """Why nothing was looked for (not found), or the error (can't draw)."""

    @property
    def summary(self) -> str:
        """The row's problem in a few words."""
        if self.kind == CANT_DRAW:
            return f"Can't be drawn: {self.reason}"
        if not self.tried:
            return f"{self.written}: {self.reason}"
        if any(t.found for t in self.tried):
            return f"{self.written}: here now; re-read to use it"
        return f"{self.written}: not found"


def picture_problems(
    conn: Connection, root_path: Callable[[str], str | None]
) -> list[PictureProblem]:
    """Every item picture not found or that can't be drawn, by item title. ``root_path``
    gives a watched folder's path on this computer (``None`` when it has none), to look on
    disk for a tried path no scan knows."""
    found = [*_not_found(conn, root_path), *_cant_draw(conn)]
    return sorted(found, key=lambda p: (p.title.casefold(), p.entity_id, p.kind, p.relpath))


def count_picture_problems(conn: Connection) -> int:
    """How many rows :func:`picture_problems` lists (without looking at the paths)."""
    missing = select(func.count()).select_from(FileNotFound).where(_still_missing())
    failed = select(func.count()).select_from(_failures().subquery())
    return int(conn.scalar(missing) or 0) + int(conn.scalar(failed) or 0)


def _still_missing() -> ColumnElement[bool]:
    has_one = exists().where(
        EntityResource.entity_id == FileNotFound.entity_id,
        EntityResource.role == FileNotFound.role,
    )
    return ~has_one


def _not_found(conn: Connection, root_path: Callable[[str], str | None]) -> list[PictureProblem]:
    query = (
        select(
            FileNotFound.entity_id,
            Entity.title,
            Entity.type,
            FileNotFound.role,
            Resource.id,
            Resource.root_id,
            Resource.relpath,
            FileNotFound.written,
            FileNotFound.tried,
            FileNotFound.reason,
        )
        .join(Entity, Entity.id == FileNotFound.entity_id)
        .join(Resource, Resource.id == FileNotFound.resource_id)
        .where(_still_missing())
    )
    problems = []
    for row in conn.execute(query):
        paths = tuple(_state(conn, row.root_id, p, root_path) for p in row.tried or [])
        problems.append(
            PictureProblem(
                NOT_FOUND,
                row.entity_id,
                row.title,
                row.type,
                row.role,
                row.id,
                row.root_id,
                row.relpath,
                row.written,
                paths,
                row.reason,
            )
        )
    return problems


def _state(
    conn: Connection, root_id: str, relpath: str, root_path: Callable[[str], str | None]
) -> TriedPath:
    """What is at ``relpath`` in the root now, as scans know it, else on disk."""
    known = select(Resource.status, Resource.skipped, Resource.relpath).where(
        Resource.root_id == root_id, Resource.parent_resource_id.is_(None)
    )
    row = conn.execute(known.where(Resource.relpath == relpath)).first()
    if row is None:  # in another case, as resource_at matches it
        lower = known.where(func.lower(Resource.relpath) == relpath.lower())
        row = conn.execute(lower.order_by(Resource.relpath).limit(1)).first()
    if row is not None:
        status, skipped, stored = row
        if status == ResourceStatus.MISSING:
            return TriedPath(relpath, MISSING_NOW)
        if skipped:
            return TriedPath(stored, SKIPPED)
        if status == ResourceStatus.OFFLINE:
            return TriedPath(stored, OFFLINE)
        return TriedPath(stored, HERE, found=True)
    base = root_path(root_id)
    if base is None or not os.path.isdir(base):
        return TriedPath(relpath, UNREACHABLE)
    try:
        on_disk = os.path.exists(local_path(base, relpath))
    except (OSError, ValueError):
        on_disk = False
    return TriedPath(relpath, NOT_SCANNED if on_disk else NOT_THERE)


def _failures() -> Select[*tuple[Any, ...]]:
    picture = aliased(Resource)
    return (
        select(
            EntityResource.entity_id,
            Entity.title,
            Entity.type,
            EntityResource.role,
            picture.id,
            picture.root_id,
            picture.relpath,
            PictureFailure.error,
        )
        .join(picture, picture.id == PictureFailure.resource_id)
        .join(EntityResource, EntityResource.resource_id == picture.id)
        .join(Entity, Entity.id == EntityResource.entity_id)
        .where(
            picture.status != ResourceStatus.MISSING,
            picture.size.is_not_distinct_from(PictureFailure.size),
            picture.mtime_ns.is_not_distinct_from(PictureFailure.mtime_ns),
        )
    )


def _cant_draw(conn: Connection) -> list[PictureProblem]:
    return [
        PictureProblem(
            CANT_DRAW, entity_id, title, type_, role, rid, root_id, relpath, reason=error
        )
        for entity_id, title, type_, role, rid, root_id, relpath, error in conn.execute(_failures())
    ]
