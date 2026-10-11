"""Managing a keep's roots (DESIGN.md §4, §12 "Keep configuration").

Edits to ``keep.toml`` (:func:`add_root`, :func:`edit_root`) work on a copy of the
:class:`~tagalot.core.keep.KeepConfig` and check it before anything is written; the
session saves the result. The database side:

- :func:`root_statuses` gives each root's status for the window.
- **Stop watching** (:func:`unwatch`) keeps everything: the root stays in ``keep.toml`` with
  ``watched = false``, is skipped by scans, and its files are marked offline. Watching it
  again (or adding the same folder) and scanning reconnects them, tags and all.
- **Removing with its items** (:func:`removal_counts`, :func:`delete_root`) deletes the
  root's files from the keep, the items that had files only there, and containers that are
  left with nothing (an artist with no albums). Files on disk are never touched.
"""

import copy
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import ModuleType

from sqlalchemy import Connection, delete, func, select, update

from tagalot.core import closure, fts
from tagalot.core.ingest import theme_text_source
from tagalot.core.keep import (
    DEFAULT_EXCLUDES,
    KeepConfig,
    KeepError,
    RootConfig,
    clean_relpath,
    nested_paths,
    root_id_for,
    validate_keep_config,
)
from tagalot.core.models import (
    Entity,
    EntityAncestor,
    EntityContains,
    EntityResource,
    Resource,
    ResourceStatus,
    Root,
)
from tagalot.core.theme_schema import ThemeSchema

UNWATCHED_ERROR = "Not watched"
"""The root's ``last_error`` while it isn't watched."""


# --- keep.toml ---


def add_root(config: KeepConfig, keep_dir: Path, name: str, path: str) -> KeepConfig:
    """``config`` with a new root watching ``path``. Adding the folder of a root that isn't
    watched watches that root again instead (its items reconnect at the next scan). Raises
    :class:`KeepError` with a message for the user."""
    path = path.strip()
    if not path:
        raise KeepError("Choose a folder to watch.")
    for root in config.roots:
        if _same_path(root.path, path):
            if root.watched:
                raise KeepError(f"{root.name!r} already watches {path}.")
            return edit_root(config, keep_dir, root.id, watched=True)
    root = RootConfig(
        id=root_id_for(path, (r.id for r in config.roots)),
        name=name.strip() or path,
        path=path,
        exclude=list(DEFAULT_EXCLUDES),
    )
    new = replace(config, roots=[*copy.deepcopy(config.roots), root])
    _check(new, keep_dir, root)
    return new


def edit_root(
    config: KeepConfig,
    keep_dir: Path,
    root_id: str,
    *,
    name: str | None = None,
    path: str | None = None,
    exclude: list[str] | None = None,
    exclude_notes: dict[str, str] | None = None,
    watched: bool | None = None,
    writable: bool | None = None,
) -> KeepConfig:
    """``config`` with one root changed. Raises :class:`KeepError` for a bad value."""
    roots = copy.deepcopy(config.roots)
    root = next((r for r in roots if r.id == root_id), None)
    if root is None:
        raise KeepError(f"This keep has no root {root_id!r}.")
    if name is not None:
        if not name.strip():
            raise KeepError("A root needs a name.")
        root.name = name.strip()
    if path is not None:
        if not path.strip():
            raise KeepError("A root needs a folder.")
        root.path = path.strip()
    if exclude is not None:
        root.exclude = [p.strip() for p in exclude if p.strip()]
    if exclude_notes is not None:
        root.exclude_notes = {p.strip(): n.strip() for p, n in exclude_notes.items() if n.strip()}
    root.exclude_notes = {p: n for p, n in root.exclude_notes.items() if p in root.exclude}
    if watched is not None:
        root.watched = watched
    if writable is not None:
        root.writable = writable
    new = replace(config, roots=roots)
    _check(new, keep_dir, root)
    return new


NOTE_MARK = " # "
"""Between a Skip pattern and its note, in Keep configuration's Skip box (#334)."""


def skip_lines(root: RootConfig) -> list[str]:
    """A root's Skip patterns as the Skip box shows them: ``pattern  # note``."""
    return [
        f"{p}{NOTE_MARK}{root.exclude_notes[p]}" if root.exclude_notes.get(p) else p
        for p in root.exclude
    ]


def parse_skip_lines(lines: Iterable[str]) -> tuple[list[str], dict[str, str]]:
    """The Skip box's lines as patterns and their notes: a note follows `` # `` (a pattern
    written by Tagalot brackets a ``#`` in a name, so it never contains one)."""
    patterns: list[str] = []
    notes: dict[str, str] = {}
    for line in lines:
        pattern, _, note = line.partition(NOTE_MARK)
        pattern = pattern.strip()
        if not pattern or pattern.startswith("#"):
            continue
        patterns.append(pattern)
        if note.strip():
            notes[pattern] = note.strip()
    return patterns, notes


def add_skipped(
    config: KeepConfig, keep_dir: Path, root_id: str, patterns: Iterable[str], note: str
) -> KeepConfig:
    """``config`` with these patterns added to a root's Skip list, with ``note`` saying why
    (a pattern already there keeps its note)."""
    root = next((r for r in config.roots if r.id == root_id), None)
    if root is None:
        raise KeepError(f"This keep has no root {root_id!r}.")
    new = [p for p in dict.fromkeys(patterns) if p not in root.exclude]
    notes = dict(root.exclude_notes) | {p: note for p in new}
    return edit_root(config, keep_dir, root_id, exclude=[*root.exclude, *new], exclude_notes=notes)


def without_root(config: KeepConfig, root_id: str) -> KeepConfig:
    """``config`` with a root removed from ``keep.toml`` (and the notes folder, if it was
    there)."""
    roots = [copy.deepcopy(r) for r in config.roots if r.id != root_id]
    if config.notes_root == root_id:
        return replace(config, roots=roots, notes_root=None, notes_path="")
    return replace(config, roots=roots)


def folder_in_root(folder: str, root_path: str, pathmod: ModuleType = os.path) -> str | None:
    """Where ``folder`` is inside the root at ``root_path``, as a root-relative POSIX path
    (``""``: the root itself), or ``None`` if it isn't inside. Paths are compared as
    ``pathmod`` does (``ntpath``: ignoring case, either slash, UNC shares)."""
    if not folder.strip() or not root_path.strip():
        return None
    inner, outer = pathmod.normpath(folder), pathmod.normpath(root_path)
    a, b = pathmod.normcase(inner), pathmod.normcase(outer)
    if a == b:
        return ""
    prefix = b if b.endswith(pathmod.sep) else b + pathmod.sep
    if not a.startswith(prefix):
        return None
    return clean_relpath(inner[len(prefix) :].replace(pathmod.sep, "/"))


def notes_folder_for(
    config: KeepConfig,
    root_path: Callable[[str], str],
    folder: str,
    pathmod: ModuleType = os.path,
) -> tuple[str, str] | None:
    """The root (``root_path`` gives this computer's path for each) holding ``folder``, and
    the folder's path in it, or ``None`` when no watched folder holds it."""
    for root in config.roots:
        inside = folder_in_root(folder, root_path(root.id), pathmod)
        if inside is not None:
            return root.id, inside
    return None


def with_notes_folder(config: KeepConfig, root_id: str | None, path: str = "") -> KeepConfig:
    """``config`` with its notes folder at ``path`` in the root ``root_id`` (§9 *The notes
    folder*), or none (``None``)."""
    if root_id is None:
        return replace(config, notes_root=None, notes_path="", roots=copy.deepcopy(config.roots))
    if root_id not in {r.id for r in config.roots}:
        raise KeepError(f"The keep has no watched folder {root_id!r}.")
    return replace(
        config,
        notes_root=root_id,
        notes_path=clean_relpath(path),
        roots=copy.deepcopy(config.roots),
    )


def _check(config: KeepConfig, keep_dir: Path, root: RootConfig) -> None:
    validate_keep_config(config, keep_dir / "keep.toml")
    if nested_paths(keep_dir, Path(root.path)):
        raise KeepError(
            f"{root.path} and the keep folder are inside one another; the keep would scan "
            "its own files."
        )
    for other in config.roots:
        if other.id != root.id and nested_paths(Path(other.path), Path(root.path)):
            raise KeepError(
                f"{root.path} and {other.name!r} ({other.path}) are inside one another; "
                "their files would be listed twice."
            )


def _same_path(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return Path(a) == Path(b)


# --- status ---


@dataclass(frozen=True)
class RootStatus:
    """What the window shows for one root."""

    root_id: str
    online: bool
    last_scan_at: datetime | None
    last_error: str | None
    files: int
    """Files (and folders) known in it, whatever their status."""
    offline: int
    missing: int
    skipped: int = 0
    """Known files the scans now leave out (#152)."""


def root_statuses(conn: Connection) -> dict[str, RootStatus]:
    """Every root's status from the ``root`` table and its resources."""
    counts: dict[tuple[str, ResourceStatus], int] = {
        (root_id, status): n
        for root_id, status, n in conn.execute(
            select(Resource.root_id, Resource.status, func.count()).group_by(
                Resource.root_id, Resource.status
            )
        )
    }
    skipped = dict(
        conn.execute(
            select(Resource.root_id, func.count())
            .where(Resource.skipped.is_(True))
            .group_by(Resource.root_id)
        ).all()
    )
    found = {}
    for root_id, online, scanned, error in conn.execute(
        select(Root.id, Root.online, Root.last_scan_at, Root.last_error)
    ):

        def n(status: ResourceStatus, root_id: str = root_id) -> int:
            return counts.get((root_id, status), 0)

        found[root_id] = RootStatus(
            root_id,
            online,
            scanned,
            error,
            files=sum(n(s) for s in ResourceStatus),
            offline=n(ResourceStatus.OFFLINE),
            missing=n(ResourceStatus.MISSING),
            skipped=int(skipped.get(root_id, 0)),
        )
    return found


# --- stop watching ---


def unwatch(conn: Connection, root_id: str) -> int:
    """Mark a root not watched: its ``ok`` files become offline (nothing is deleted).
    Returns how many."""
    conn.execute(
        update(Root).where(Root.id == root_id).values(online=False, last_error=UNWATCHED_ERROR)
    )
    return conn.execute(
        update(Resource)
        .where(Resource.root_id == root_id, Resource.status == ResourceStatus.OK)
        .values(status=ResourceStatus.OFFLINE)
    ).rowcount


# --- removing a root with its items ---


@dataclass(frozen=True)
class RemovalCounts:
    deleted: int
    """Items that go: those with files only in this root, and containers left empty."""
    kept: int
    """Items with files here that also have files in other roots (they lose these)."""


def removal_counts(conn: Connection, root_id: str) -> RemovalCounts:
    doomed, kept = _doomed(conn, root_id)
    return RemovalCounts(len(doomed), len(kept))


def delete_root(conn: Connection, schema: ThemeSchema, root_id: str) -> RemovalCounts:
    """Delete a root's files, the items that had files only there, and containers left
    empty, and the root's row; see the module docstring."""
    doomed, kept = _doomed(conn, root_id)
    ids = sorted(doomed)
    if ids:
        closure.detach(conn, ids)
        for start in range(0, len(ids), 500):
            conn.execute(delete(Entity).where(Entity.id.in_(ids[start : start + 500])))
    conn.execute(delete(Resource).where(Resource.root_id == root_id))
    conn.execute(delete(Root).where(Root.id == root_id))
    fts.sync_entities(conn, ids, theme_text_source(schema))
    return RemovalCounts(len(doomed), len(kept))


def _doomed(conn: Connection, root_id: str) -> tuple[set[int], set[int]]:
    """(items to delete, items that keep files elsewhere) for removing ``root_id``."""
    here = select(Resource.id).where(Resource.root_id == root_id)
    linked = set(
        conn.scalars(select(EntityResource.entity_id).where(EntityResource.resource_id.in_(here)))
    )
    elsewhere = set(
        conn.scalars(
            select(EntityResource.entity_id)
            .join(Resource, Resource.id == EntityResource.resource_id)
            .where(EntityResource.entity_id.in_(linked), Resource.root_id != root_id)
        )
    )
    doomed = linked - elsewhere
    # Containers above them that would be left with no files and nothing inside.
    candidates = (
        set(
            conn.scalars(
                select(EntityAncestor.ancestor_id).where(
                    EntityAncestor.entity_id.in_(doomed), EntityAncestor.depth > 0
                )
            )
        )
        - doomed
    )
    with_files = set(
        conn.scalars(
            select(EntityResource.entity_id).where(
                EntityResource.entity_id.in_(candidates),
                EntityResource.resource_id.not_in(here),
            )
        )
    )
    children: dict[int, set[int]] = {}
    for parent, child in conn.execute(
        select(EntityContains.parent_id, EntityContains.child_id).where(
            EntityContains.parent_id.in_(candidates)
        )
    ):
        children.setdefault(parent, set()).add(child)
    changed = True
    while changed:
        changed = False
        for entity in candidates - doomed - with_files:
            if children.get(entity, set()) <= doomed:
                doomed.add(entity)
                changed = True
    return doomed, linked & elsewhere
