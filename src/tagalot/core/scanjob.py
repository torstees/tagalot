"""One complete scan of a root, in the order of DESIGN.md §6 steps 1-4.

:func:`scan_root` runs in a worker: file-system work happens on the calling thread, reads use
a read-only engine, and every write goes through the :class:`~tagalot.core.writer.DbWriter`.
Ingest, closure maintenance, and thumbnails (steps 5-7) arrive with themes (M4) and
thumbnails (M8).
"""

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial

from sqlalchemy import Connection, Engine

from tagalot.core.fingerprint import (
    FingerprintJob,
    FingerprintResult,
    compute_fingerprints,
    pending_fingerprints,
    store_fingerprints,
)
from tagalot.core.keep import RootConfig
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
from tagalot.core.writer import DbWriter

logger = logging.getLogger(__name__)


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
) -> ScanReport:
    """Scan one root. ``path`` is this machine's path for it (after per-user overrides).

    An unreachable root is recorded as offline and its resources marked offline; nothing is
    deleted and the scan stops there. Otherwise the root is walked, diffed, and applied in
    short transactions; this root's unfingerprinted files are hashed (once each, so an
    unreadable file can't loop); and moves among the new files are reattached.
    """
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
    logger.info(
        "Scanned %s: %d new, %d changed, %d restored, %d missing, %d moved, %d fingerprinted",
        root.id,
        report.new,
        report.changed,
        report.restored,
        report.missing,
        len(report.moves),
        report.fingerprinted,
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
