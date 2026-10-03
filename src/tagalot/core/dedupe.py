"""Exact duplicates (DESIGN.md §13; #117).

Files are **exact duplicates** when their fingerprints match (size, first and last 64 KiB;
§5): cheap, and nearly always right. :func:`exact_groups` lists them, most space wasted
first. To be sure, :func:`verify_group` reads every file of a group whole and compares
full hashes, splitting the group if some differ in the middle. That reads whole files
(slow on a share), so it runs only when asked, in a worker; results are kept for the
session (:class:`Verifier`), keyed by each file's size and time as it is when checked
(so a file changed since the last scan is read again), not in the keep.

**Near-duplicates** (:func:`near_duplicates`) are items the theme finds alike: it gives each
item cheap blocking keys from its fields, only items of a type that share a key are scored
with its ``similarity``, and pairs at or above its threshold are listed, most alike first.

Comparing items side by side is ``core.compare`` (#119), merging them ``core.merge``
(#120), and marking them not duplicates ``core.not_duplicates`` (#121).
"""

import hashlib
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import Connection, Table, func, select

from tagalot.core.models import Entity, EntityResource, Resource, ResourceKind, ResourceStatus, Root
from tagalot.core.roots import local_path
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import EntityRef, Record

logger = logging.getLogger(__name__)

CHUNK = 1 << 20
"""Bytes read at a time for a full hash."""


@dataclass(frozen=True)
class DuplicateFile:
    resource_id: int
    root_id: str
    root_name: str
    relpath: str
    size: int
    mtime_ns: int | None
    status: ResourceStatus
    path: str | None
    """This computer's path, or ``None`` if its root has none here."""
    items: tuple[tuple[int, str], ...] = ()
    """``(entity id, title)`` of the items using it."""


@dataclass(frozen=True)
class DuplicateGroup:
    fingerprint: bytes
    size: int
    files: tuple[DuplicateFile, ...]

    @property
    def wasted(self) -> int:
        """Bytes the extra copies take."""
        return self.size * (len(self.files) - 1)


def exact_groups(conn: Connection, root_path: Callable[[str], str | None]) -> list[DuplicateGroup]:
    """Files that share a fingerprint, grouped, most space wasted first. Missing and
    skipped files are left out; offline ones are listed (they can't be verified)."""
    usable = (
        Resource.kind == ResourceKind.FILE,
        Resource.fingerprint.is_not(None),
        Resource.status != ResourceStatus.MISSING,
        Resource.skipped.is_(False),
        Resource.parent_resource_id.is_(None),
    )
    shared = (
        select(Resource.fingerprint)
        .where(*usable)
        .group_by(Resource.fingerprint)
        .having(func.count() > 1)
    )
    rows = conn.execute(
        select(
            Resource.id,
            Resource.root_id,
            Root.name,
            Resource.relpath,
            Resource.size,
            Resource.mtime_ns,
            Resource.status,
            Resource.fingerprint,
        )
        .join(Root, Root.id == Resource.root_id)
        .where(*usable, Resource.fingerprint.in_(shared))
        .order_by(Resource.fingerprint, Root.name, Resource.relpath)
    ).all()
    items: dict[int, list[tuple[int, str]]] = {}
    ids = [row.id for row in rows]
    for start in range(0, len(ids), 500):
        for resource_id, entity_id, title in conn.execute(
            select(EntityResource.resource_id, Entity.id, Entity.title)
            .join(Entity, Entity.id == EntityResource.entity_id)
            .where(EntityResource.resource_id.in_(ids[start : start + 500]))
            .order_by(Entity.title)
        ):
            items.setdefault(resource_id, []).append((entity_id, title))
    groups: dict[bytes, list[DuplicateFile]] = {}
    for row in rows:
        base = root_path(row.root_id)
        groups.setdefault(row.fingerprint, []).append(
            DuplicateFile(
                row.id,
                row.root_id,
                row.name,
                row.relpath,
                row.size or 0,
                row.mtime_ns,
                row.status,
                local_path(base, row.relpath) if base is not None else None,
                tuple(dict.fromkeys(items.get(row.id, []))),
            )
        )
    found = [DuplicateGroup(fp, files[0].size, tuple(files)) for fp, files in groups.items()]
    found.sort(key=lambda g: (-g.wasted, g.files[0].relpath.casefold()))
    return found


def full_hash(path: str) -> bytes:
    """A blake2b digest of the whole file (read in chunks)."""
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.digest()


@dataclass(frozen=True)
class Verification:
    """What reading a group's files whole found."""

    identical: tuple[tuple[int, ...], ...]
    """Resource ids with the same full contents, in sets of two or more."""
    different: tuple[int, ...]
    """Ids whose contents match no other file of the group (fingerprints collided)."""
    unread: dict[int, str] = field(default_factory=dict)
    """Ids that couldn't be read (offline, no path here, or an error), and why."""
    changed: tuple[int, ...] = ()
    """Ids whose file changed since the last scan (a scan updates the groups)."""

    @property
    def confirmed(self) -> bool:
        """Every file was read and all are the same."""
        return len(self.identical) == 1 and not self.different and not self.unread


class Verifier:
    """Full-hash verification with results kept for the session (thread-safe)."""

    def __init__(self) -> None:
        self._hashes: dict[tuple[int, int, int | None], bytes] = {}
        self._lock = threading.Lock()

    def verify_group(self, group: DuplicateGroup) -> Verification:
        """Read each available file of the group whole and compare. Does I/O: call it from
        a worker."""
        by_hash: dict[bytes, list[int]] = {}
        unread: dict[int, str] = {}
        changed: list[int] = []
        for file in group.files:
            if file.status is not ResourceStatus.OK:
                unread[file.resource_id] = "offline"
                continue
            if file.path is None:
                unread[file.resource_id] = "its folder has no path on this computer"
                continue
            try:
                # The file as it is now, not as the last scan saw it: it may have changed
                # since (a hash cached for the old contents mustn't be reused).
                st = os.stat(file.path)
                if (st.st_size, st.st_mtime_ns) != (file.size, file.mtime_ns):
                    changed.append(file.resource_id)
                key = (file.resource_id, st.st_size, st.st_mtime_ns)
                with self._lock:
                    digest = self._hashes.get(key)
                if digest is None:
                    digest = full_hash(file.path)
                    with self._lock:
                        self._hashes[key] = digest
            except OSError as e:
                unread[file.resource_id] = e.strerror or str(e)
                continue
            by_hash.setdefault(digest, []).append(file.resource_id)
        identical = tuple(tuple(ids) for ids in by_hash.values() if len(ids) > 1)
        different = tuple(ids[0] for ids in by_hash.values() if len(ids) == 1)
        return Verification(identical, different, unread, tuple(changed))


# --- near-duplicates (the theme's blocking keys and similarity) ---

MAX_BUCKET = 200
"""Items sharing one blocking key that are compared at most (a key shared by more is too
loose to be useful, and comparing every pair would be slow); bigger buckets are skipped."""


@dataclass(frozen=True)
class NearPair:
    type_id: str
    a: tuple[int, str]
    """``(entity id, title)``."""
    b: tuple[int, str]
    score: float


@dataclass
class NearDuplicates:
    pairs: list[NearPair]
    skipped_keys: int = 0
    """Blocking keys shared by more than :data:`MAX_BUCKET` items, left out."""


def near_duplicates(
    conn: Connection, schema: ThemeSchema, max_bucket: int = MAX_BUCKET
) -> NearDuplicates:
    """Pairs of items the theme finds alike, most alike first (call it from a worker)."""
    theme = schema.theme()
    threshold = schema.theme.near_duplicate_threshold
    found: list[NearPair] = []
    skipped = 0
    for entity_table in schema.entities.values():
        entity, table = entity_table.entity, entity_table.table
        records = _records(conn, entity_table.type_id, table, [f.name for f in entity_table.fields])
        buckets: dict[str, list[Record]] = {}
        for record in records:
            try:
                keys = set(theme.blocking_keys(entity, record))
            except Exception:
                logger.exception("blocking_keys failed for %s", record.ref.id)
                continue
            for key in keys:
                buckets.setdefault(key, []).append(record)
        seen: set[tuple[int, int]] = set()
        for key, members in buckets.items():
            if len(members) > max_bucket:
                skipped += 1
                logger.info(
                    "Near-duplicates: key %r is shared by %d items; skipped", key, len(members)
                )
                continue
            for i, a in enumerate(members):
                for b in members[i + 1 :]:
                    pair = (min(a.ref.id, b.ref.id), max(a.ref.id, b.ref.id))
                    if pair in seen:
                        continue
                    seen.add(pair)
                    try:
                        score = float(theme.similarity(entity, a, b))
                    except Exception:
                        logger.exception("similarity failed for %s and %s", a.ref.id, b.ref.id)
                        continue
                    if score >= threshold:
                        first, second = sorted((a, b), key=lambda r: r.ref.id)
                        found.append(
                            NearPair(
                                entity_table.type_id,
                                (first.ref.id, first.title),
                                (second.ref.id, second.title),
                                score,
                            )
                        )
    found.sort(key=lambda p: (-p.score, p.a[1].casefold(), p.b[1].casefold()))
    return NearDuplicates(found, skipped)


def _records(conn: Connection, type_id: str, table: Table, names: list[str]) -> list[Record]:
    columns = [table.c[n] for n in names]
    rows = conn.execute(
        select(Entity.id, Entity.title, *columns)
        .join(table, table.c.id == Entity.id)
        .order_by(Entity.id)
    )
    return [
        Record(EntityRef(row[0], type_id), row[1], dict(zip(names, row[2:], strict=True)), {})
        for row in rows
    ]
