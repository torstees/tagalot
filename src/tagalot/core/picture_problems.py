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

**Ignoring** (``picture_ignore``) leaves rows off the list: one item's row while it says the
same (a changed value or file shows it again), a value files name for every item (a
template's ``TBD``), or every item of a type. Each is one undo step
(:class:`PictureIgnoreChange`); the list can show ignored rows, to stop ignoring them.

Making the list stats the tried paths when nothing is known of them: call it from a worker.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sqlalchemy import ColumnElement, Connection, Select, delete, exists, func, insert, select
from sqlalchemy.orm import aliased

from tagalot.core.models import (
    Entity,
    EntityResource,
    FileNotFound,
    PictureFailure,
    PictureIgnore,
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
    marker: str = ""
    """What the row says, to ignore it for its item until that changes."""
    ignored: str | None = None
    """Why it is ignored (``item``, ``value``, or ``type``), when ignored rows are listed."""

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
    conn: Connection, root_path: Callable[[str], str | None], *, ignored: bool = False
) -> list[PictureProblem]:
    """Every item picture not found or that can't be drawn, by item title; with
    ``ignored``, the ignored ones too, saying why. ``root_path`` gives a watched folder's
    path on this computer (``None`` when it has none), to look on disk for a tried path no
    scan knows."""
    rules = picture_rules(conn)
    found = []
    for problem in [*_not_found(conn, root_path), *_cant_draw(conn)]:
        why = ignored_by(problem, rules)
        if why is None or ignored:
            found.append(replace(problem, ignored=why))
    return sorted(found, key=lambda p: (p.title.casefold(), p.entity_id, p.kind, p.relpath))


def count_picture_problems(conn: Connection) -> int:
    """How many rows :func:`picture_problems` lists, not ignored (without looking on disk)."""
    return len(picture_problems(conn, lambda root: None))


# --- ignoring ---

ITEM: Literal["item"] = "item"
VALUE: Literal["value"] = "value"
TYPE: Literal["type"] = "type"
PictureScope = Literal["item", "value", "type"]


@dataclass(frozen=True)
class PictureRule:
    """One ``picture_ignore`` row (see :class:`~tagalot.core.models.PictureIgnore`)."""

    scope: str
    entity_id: int | None = None
    marker: str | None = None
    value: str | None = None
    type: str | None = None


def rule_for(problem: PictureProblem, scope: PictureScope) -> PictureRule:
    """The rule that ignores ``problem`` for its item, its value everywhere, or its type."""
    if scope == ITEM:
        return PictureRule(ITEM, entity_id=problem.entity_id, marker=problem.marker)
    if scope == VALUE:
        if problem.kind != NOT_FOUND or not problem.written:
            raise ValueError("only a value a file names can be ignored everywhere")
        return PictureRule(VALUE, value=problem.written.strip().casefold())
    return PictureRule(TYPE, type=problem.type)


def ignored_by(problem: PictureProblem, rules: frozenset[PictureRule]) -> str | None:
    """Which kind of rule ignores ``problem`` (``item``, ``value``, ``type``), if any."""
    for scope in (ITEM, VALUE, TYPE):
        try:
            if rule_for(problem, scope) in rules:
                return scope
        except ValueError:
            continue
    return None


def picture_rules(conn: Connection) -> frozenset[PictureRule]:
    columns = (
        PictureIgnore.scope,
        PictureIgnore.entity_id,
        PictureIgnore.marker,
        PictureIgnore.value,
        PictureIgnore.type,
    )
    return frozenset(PictureRule(*row) for row in conn.execute(select(*columns)))


@dataclass(frozen=True)
class PictureIgnoreChange:
    """Rules added and removed by one ignore or stop-ignoring; one undo step."""

    label: str
    added: frozenset[PictureRule] = field(default_factory=frozenset)
    removed: frozenset[PictureRule] = field(default_factory=frozenset)


def ignore_pictures(
    conn: Connection, problems: list[PictureProblem], scope: PictureScope, label: str
) -> PictureIgnoreChange:
    """Ignore these rows for their items, their values everywhere, or their types."""
    rules = frozenset(rule_for(p, scope) for p in problems) - picture_rules(conn)
    change = PictureIgnoreChange(label, added=rules)
    restore_picture_ignores(conn, change, forward=True)
    return change


def stop_ignoring(conn: Connection, problem: PictureProblem, label: str) -> PictureIgnoreChange:
    """Stop ignoring a row: remove every rule that leaves it off the list."""
    rules = picture_rules(conn)
    matching = set()
    for scope in (ITEM, VALUE, TYPE):
        try:
            rule = rule_for(problem, scope)
        except ValueError:
            continue
        if rule in rules:
            matching.add(rule)
    change = PictureIgnoreChange(label, removed=frozenset(matching))
    restore_picture_ignores(conn, change, forward=True)
    return change


def restore_picture_ignores(
    conn: Connection, change: PictureIgnoreChange, *, forward: bool
) -> None:
    """Apply (``forward``) or undo a :class:`PictureIgnoreChange`."""
    add, remove = (change.added, change.removed) if forward else (change.removed, change.added)
    for rule in remove:
        conn.execute(
            delete(PictureIgnore).where(
                PictureIgnore.scope == rule.scope,
                PictureIgnore.entity_id.is_not_distinct_from(rule.entity_id),
                PictureIgnore.marker.is_not_distinct_from(rule.marker),
                PictureIgnore.value.is_not_distinct_from(rule.value),
                PictureIgnore.type.is_not_distinct_from(rule.type),
            )
        )
    for rule in add:
        gone = select(Entity.id).where(Entity.id == rule.entity_id)
        if rule.entity_id is not None and conn.scalar(gone) is None:
            continue  # an item deleted since can't be ignored
        conn.execute(
            insert(PictureIgnore).values(
                scope=rule.scope,
                entity_id=rule.entity_id,
                marker=rule.marker,
                value=rule.value,
                type=rule.type,
            )
        )


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
                marker=f"{NOT_FOUND}:{row.id}:{row.role}:{row.written}",
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
            PictureFailure.size,
            PictureFailure.mtime_ns,
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
            CANT_DRAW,
            row.entity_id,
            row.title,
            row.type,
            row.role,
            row.id,
            row.root_id,
            row.relpath,
            reason=row.error,
            marker=f"{CANT_DRAW}:{row.id}:{row.size}:{row.mtime_ns}",
        )
        for row in conn.execute(_failures())
    ]
