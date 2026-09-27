"""One complete scan of a root, in the order of DESIGN.md §6 steps 1-6.

:func:`scan_root` runs in a worker: file-system work happens on the calling thread, reads use
a read-only engine, and every write goes through the :class:`~tagalot.core.writer.DbWriter`.
Thumbnails (step 7) arrive in M8.
"""

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial

from sqlalchemy import Connection, Engine, select, update

from tagalot.core.fingerprint import (
    FingerprintJob,
    FingerprintResult,
    compute_fingerprints,
    pending_fingerprints,
    store_fingerprints,
)
from tagalot.core.ingest import IngestSession, IngestWarning
from tagalot.core.keep import RootConfig
from tagalot.core.models import Resource, ResourceStatus
from tagalot.core.roots import RootCheck, check_root, local_path, record_root_check, sync_roots
from tagalot.core.scanner import (
    BATCH_SIZE,
    AppliedDiff,
    DirRule,
    Move,
    RootDiff,
    apply_diff,
    detect_moves,
    diff_root,
    load_known,
    merge_applied,
    split_diff,
    walk_root,
)
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import ResourceInfo, Theme

logger = logging.getLogger(__name__)

INGEST_BATCH = 100
"""Resources per ingest transaction: the theme's ingest runs inside the writer's transaction,
so batches stay small to keep the write lock short."""


@dataclass
class ScanReport:
    """What a scan did, for the activity panel and logs."""

    root_id: str
    online: bool
    error: str | None = None
    """Why the root was offline, if it was."""
    new: int = 0
    changed: int = 0
    restored: int = 0
    missing: int = 0
    unchanged: int = 0
    fingerprinted: int = 0
    moves: list[Move] = field(default_factory=list)
    read_errors: list[tuple[str, str]] = field(default_factory=list)
    """``(relative path, message)`` for folders and files that could not be read."""
    ingested: int = 0
    """Resources the theme ingested successfully."""
    ingest_errors: list[tuple[str, str]] = field(default_factory=list)
    """``(relative path, message)`` for resources whose ingest failed; they stay pending."""
    ingest_warnings: list[IngestWarning] = field(default_factory=list)


def scan_root(
    writer: DbWriter,
    reader: Engine,
    root: RootConfig,
    path: str,
    *,
    extensions: Collection[str] | None = None,
    dirs: DirRule = False,
    when: datetime | None = None,
    check: Callable[[str], RootCheck] = check_root,
    theme: type[Theme] | None = None,
    schema: ThemeSchema | None = None,
) -> ScanReport:
    """Scan one root. ``path`` is this machine's path for it (after per-user overrides).

    An unreachable root is recorded as offline and its resources marked offline; nothing is
    deleted and the scan stops there. Otherwise the root is walked, diffed, and applied in
    short transactions; this root's unfingerprinted files are hashed (once each, so an
    unreadable file can't loop); moves among the new files are reattached; and, with a
    ``theme`` (and its ``schema``), every pending resource is ingested. The theme's
    extensions and directory rule then replace ``extensions`` and ``dirs``.
    """
    if (theme is None) != (schema is None):
        raise ValueError("pass both theme and schema, or neither")
    if theme is not None:
        extensions = theme.extensions or None
        dirs = theme.dirs
    when = when or datetime.now(UTC)
    writer.run(partial(_sync_one, root))

    reachability = check(path)
    writer.run(partial(_record_check, root.id, reachability))
    if not reachability.online:
        logger.warning("Skipping scan of %s: %s", root.id, reachability.error)
        return ScanReport(root.id, online=False, error=reachability.error)

    report = ScanReport(root.id, online=True)
    unreadable: list[str] = []

    def on_read_error(relpath: str, error: OSError) -> None:
        unreadable.append(relpath)
        report.read_errors.append((relpath, str(error)))
        logger.warning("Cannot read %s in root %s: %s", relpath or "(root)", root.id, error)

    entries = list(
        walk_root(
            path, exclude=root.exclude, extensions=extensions, dirs=dirs, on_error=on_read_error
        )
    )
    with reader.connect() as conn:
        diff = diff_root(load_known(conn, root.id), entries, unreadable)
    report.new, report.changed = len(diff.new), len(diff.changed)
    report.restored, report.missing = len(diff.restored), len(diff.missing)
    report.unchanged = len(diff.unchanged)

    futures = [writer.submit(partial(_apply, root.id, part, when)) for part in split_diff(diff)]
    applied = merge_applied(f.result() for f in futures)

    with reader.connect() as conn:
        jobs = pending_fingerprints(conn, root_id=root.id, limit=None)

    def job_path(job: FingerprintJob) -> str:
        return local_path(path, job.relpath)

    def on_hash_error(job: FingerprintJob, error: OSError) -> None:
        report.read_errors.append((job.relpath, str(error)))
        logger.warning("Cannot fingerprint %s in root %s: %s", job.relpath, root.id, error)

    batch: list[FingerprintResult] = []
    for result in compute_fingerprints(jobs, job_path, on_hash_error):
        batch.append(result)
        if len(batch) >= BATCH_SIZE:
            report.fingerprinted += writer.run(partial(_store, batch))
            batch = []
    if batch:
        report.fingerprinted += writer.run(partial(_store, batch))

    report.moves = writer.run(partial(_moves, applied))
    if theme is not None and schema is not None:
        _ingest_pending(writer, reader, root, path, theme, schema, when, report)
    logger.info(
        "Scanned %s: %d new, %d changed, %d restored, %d missing, %d moved, %d fingerprinted, "
        "%d ingested, %d ingest errors",
        root.id,
        report.new,
        report.changed,
        report.restored,
        report.missing,
        len(report.moves),
        report.fingerprinted,
        report.ingested,
        len(report.ingest_errors),
    )
    return report


# Writer jobs: module-level functions so each is a plain Callable[[Connection], T].


def _sync_one(root: RootConfig, conn: Connection) -> None:
    sync_roots(conn, [root])


def _record_check(root_id: str, check: RootCheck, conn: Connection) -> int:
    return record_root_check(conn, root_id, check)


def _apply(root_id: str, part: RootDiff, when: datetime, conn: Connection) -> AppliedDiff:
    return apply_diff(conn, root_id, part, when)


def _store(results: list[FingerprintResult], conn: Connection) -> int:
    return store_fingerprints(conn, results)


def _moves(applied: AppliedDiff, conn: Connection) -> list[Move]:
    return detect_moves(conn, applied.new_ids.values())


# --- Ingest (DESIGN.md §6 step 5) ---


def _ingest_pending(
    writer: DbWriter,
    reader: Engine,
    root: RootConfig,
    path: str,
    theme: type[Theme],
    schema: ThemeSchema,
    when: datetime,
    report: ScanReport,
) -> None:
    """Hand every pending resource of the root to the theme, in batches.

    A batch that fails rolls back and each of its resources is retried alone, so one bad
    file can't cost the rest; resources that still fail stay pending for the next scan.
    """
    with reader.connect() as conn:
        rows = conn.execute(
            select(
                Resource.id,
                Resource.relpath,
                Resource.kind,
                Resource.ext,
                Resource.size,
                Resource.mtime_ns,
            )
            .where(
                Resource.root_id == root.id,
                Resource.status == ResourceStatus.OK,
                Resource.ingested_at.is_(None),
                Resource.parent_resource_id.is_(None),
            )
            .order_by(Resource.relpath)
        ).all()
    pending = [
        ResourceInfo(rid, root.id, rel, kind.value, ext, size, mtime, local_path(path, rel))
        for rid, rel, kind, ext, size, mtime in rows
    ]
    ingester = theme()
    for start in range(0, len(pending), INGEST_BATCH):
        batch = pending[start : start + INGEST_BATCH]
        try:
            _record(report, writer.run(partial(_ingest, ingester, schema, batch, when)), batch)
        except Exception:
            for info in batch:
                try:
                    _record(
                        report, writer.run(partial(_ingest, ingester, schema, [info], when)), [info]
                    )
                except Exception as e:
                    report.ingest_errors.append((info.relpath, f"{type(e).__name__}: {e}"))
                    logger.warning("Ingest of %s in root %s failed: %s", info.relpath, root.id, e)


def _record(report: ScanReport, warnings: list[IngestWarning], batch: list[ResourceInfo]) -> None:
    report.ingested += len(batch)
    report.ingest_warnings.extend(warnings)


def _ingest(
    ingester: Theme,
    schema: ThemeSchema,
    batch: list[ResourceInfo],
    when: datetime,
    conn: Connection,
) -> list[IngestWarning]:
    """One ingest transaction: the theme's ingest, its flush, and marking the batch done."""
    ctx = IngestSession(conn, schema)
    ingester.ingest(batch, ctx)
    flushed = ctx.flush()
    conn.execute(
        update(Resource).where(Resource.id.in_([r.id for r in batch])).values(ingested_at=when)
    )
    return flushed.warnings
