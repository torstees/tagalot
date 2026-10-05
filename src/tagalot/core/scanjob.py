"""One complete scan of a root, in the order of DESIGN.md §6 steps 1-6.

:func:`scan_root` runs in a worker: file-system work happens on the calling thread, reads use
a read-only engine, and every write goes through the :class:`~tagalot.core.writer.DbWriter`.
Thumbnails (step 7) arrive in M8.
"""

import logging
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any

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
from tagalot.core.models import Resource, ResourceStatus, Root
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
    walk_scope,
)
from tagalot.core.theme_options import effective_options
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import ResourceInfo, Theme

logger = logging.getLogger(__name__)

Progress = Callable[[str], None]
"""Receives short, human-readable status messages ("Walking Music…") during a scan."""

INGEST_BATCH = 100
"""Resources per ingest transaction: the theme's ingest runs inside the writer's transaction,
so batches stay small to keep the write lock short."""
PREPARE_THREADS = 8
"""Threads running the theme's ``prepare`` at once (#274): reading files is mostly waiting
(a share's latency) or decoding that releases the GIL, so threads overlap it."""
PREPARE_CHUNK = 10
"""Resources per ``prepare`` call, so one ingest batch is read by several threads."""

Prepared = tuple[list[ResourceInfo], dict[int, Any], list[tuple[str, str]]]
"""What reading some resources gave: those read (in order), their values, and the
``(relpath, error)`` of each that couldn't be read."""


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
    skipped: int = 0
    """Known files the scan now leaves out (excludes, extensions): flagged, not missing."""
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
    theme_options: Mapping[str, Any] | None = None,
    progress: Progress | None = None,
) -> ScanReport:
    """Scan one root. ``path`` is this machine's path for it (after per-user overrides).

    An unreachable root is recorded as offline and its resources marked offline; nothing is
    deleted and the scan stops there. Otherwise the root is walked, diffed, and applied in
    short transactions; this root's unfingerprinted files are hashed (once each, so an
    unreadable file can't loop); moves among the new files are reattached; and, with a
    ``theme`` (and its ``schema``), every pending resource is ingested. The theme's
    extensions and directory rule then replace ``extensions`` and ``dirs``.

    ``theme_options`` are the keep's ``[theme.options]``; with the root's own options they
    give the values the theme sees. When those differ from the values the root was last
    ingested with, every file of the root is ingested again.
    """
    if (theme is None) != (schema is None):
        raise ValueError("pass both theme and schema, or neither")
    if theme is not None:
        extensions = theme.extensions or None
        dirs = theme.dirs
    say: Progress = progress or (lambda message: None)
    when = when or datetime.now(UTC)
    writer.run(partial(_sync_one, root))

    say(f"Checking {root.name}…")
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

    say(f"Walking {root.name}…")
    entries = list(
        walk_root(
            path, exclude=root.exclude, extensions=extensions, dirs=dirs, on_error=on_read_error
        )
    )
    with reader.connect() as conn:
        scope = walk_scope(root.exclude, extensions, dirs)
        diff = diff_root(load_known(conn, root.id), entries, unreadable, scope)
    report.new, report.changed = len(diff.new), len(diff.changed)
    report.restored, report.missing = len(diff.restored), len(diff.missing)
    report.skipped = len(diff.skipped)
    report.unchanged = len(diff.unchanged)

    say(f"Updating {root.name}: {len(diff.new)} new, {len(diff.changed)} changed…")
    futures = [writer.submit(partial(_apply, root.id, part, when)) for part in split_diff(diff)]
    applied = merge_applied(f.result() for f in futures)

    with reader.connect() as conn:
        jobs = pending_fingerprints(conn, root_id=root.id, limit=None)
    if jobs:
        say(f"Fingerprinting {len(jobs)} files in {root.name}…")

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
        options = effective_options(theme, theme_options or {}, root.options)
        for problem in options.problems:
            logger.warning("Root %s: %s", root.id, problem)
            report.ingest_warnings.append(IngestWarning(root.id, None, problem))
        defaults = effective_options(theme, {}, {}).fingerprint(1)  # recorded before versions
        if writer.run(
            partial(_options_changed, root.id, options.fingerprint(theme.version), defaults)
        ):
            say(f"Theme options changed: ingesting {root.name} again…")
        _ingest_pending(
            writer, reader, root, path, theme, schema, when, report, say, options.values
        )
        writer.run(partial(_record_options, root.id, options.fingerprint(theme.version)))
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


def _options_changed(root_id: str, fingerprint: str, defaults: str, conn: Connection) -> bool:
    """If the root was ingested with other option values, mark its files pending. A root
    with nothing recorded (new, or scanned before options existed) was ingested with the
    defaults, if at all."""
    stored = conn.scalar(select(Root.ingest_options).where(Root.id == root_id))
    if (stored if stored is not None else defaults) == fingerprint:
        return False
    marked = conn.execute(
        update(Resource)
        .where(Resource.root_id == root_id, Resource.ingested_at.is_not(None))
        .values(ingested_at=None)
    ).rowcount
    return bool(marked)


def _record_options(root_id: str, fingerprint: str, conn: Connection) -> None:
    conn.execute(update(Root).where(Root.id == root_id).values(ingest_options=fingerprint))


# --- Ingest (DESIGN.md §6 step 5) ---


def ingest_pending(
    writer: DbWriter,
    reader: Engine,
    root: RootConfig,
    path: str,
    *,
    theme: type[Theme],
    schema: ThemeSchema,
    theme_options: Mapping[str, Any] | None = None,
    when: datetime | None = None,
    progress: Progress | None = None,
) -> ScanReport:
    """Ingest the root's pending resources now, without walking it (re-reading chosen files,
    DESIGN.md §6). The root's theme options apply as in a scan."""
    report = ScanReport(root.id, online=True)
    options = effective_options(theme, theme_options or {}, root.options)
    _ingest_pending(
        writer,
        reader,
        root,
        path,
        theme,
        schema,
        when or datetime.now(UTC),
        report,
        progress or (lambda message: None),
        options.values,
    )
    return report


def _ingest_pending(
    writer: DbWriter,
    reader: Engine,
    root: RootConfig,
    path: str,
    theme: type[Theme],
    schema: ThemeSchema,
    when: datetime,
    report: ScanReport,
    say: Progress,
    options: Mapping[str, Any],
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
                Resource.skipped.is_(False),
                Resource.ingested_at.is_(None),
                Resource.parent_resource_id.is_(None),
            )
            .order_by(Resource.relpath)
        ).all()
    pending = [
        ResourceInfo(rid, root.id, rel, kind.value, ext, size, mtime, local_path(path, rel))
        for rid, rel, kind, ext, size, mtime in rows
    ]
    pending = _read_last(reader, root, path, theme, pending)
    ingester = theme()
    reads = type(ingester).prepare is not Theme.prepare
    batches = [pending[i : i + INGEST_BATCH] for i in range(0, len(pending), INGEST_BATCH)]
    if not batches:
        return
    # The next batch is read (on several threads) while the writer ingests this one.
    pool = ThreadPoolExecutor(PREPARE_THREADS, thread_name_prefix="prepare") if reads else None

    def start_reading(batch: list[ResourceInfo]) -> list[Future[Prepared]]:
        if pool is None:
            return []
        chunks = [batch[i : i + PREPARE_CHUNK] for i in range(0, len(batch), PREPARE_CHUNK)]
        return [pool.submit(_prepare, ingester, chunk, root) for chunk in chunks]

    try:
        reading = start_reading(batches[0])
        for number, batch in enumerate(batches):
            done = f"{number * INGEST_BATCH + len(batch)} of {len(pending)} in {root.name}"
            prepared: dict[int, Any] = {}
            if pool is not None:
                say(f"Reading {done}…")
                batch, prepared = _gather(reading, report)
                if number + 1 < len(batches):
                    reading = start_reading(batches[number + 1])
            say(f"Ingesting {done}…")
            _ingest_batch(writer, ingester, schema, batch, when, prepared, options, report, root)
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)


def _read_last(
    reader: Engine,
    root: RootConfig,
    path: str,
    theme: type[Theme],
    pending: list[ResourceInfo],
) -> list[ResourceInfo]:
    """``pending`` with the theme's ``read_last`` files after the others; when there are
    others, with every one of the root's ``read_last`` files, read again, so a library
    export meets the files added since (DESIGN.md §6, #320)."""
    late_extensions = frozenset(theme.read_last)
    if not late_extensions:
        return pending
    first = [r for r in pending if r.ext not in late_extensions]
    late = [r for r in pending if r.ext in late_extensions]
    if not first:
        return late
    seen = {r.id for r in late}
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
                Resource.skipped.is_(False),
                Resource.parent_resource_id.is_(None),
                Resource.ext.in_(sorted(late_extensions)),
                Resource.ingested_at.is_not(None),
            )
            .order_by(Resource.relpath)
        ).all()
    late += [
        ResourceInfo(rid, root.id, rel, kind.value, ext, size, mtime, local_path(path, rel))
        for rid, rel, kind, ext, size, mtime in rows
        if rid not in seen
    ]
    late.sort(key=lambda r: r.relpath)
    return first + late


def _ingest_batch(
    writer: DbWriter,
    ingester: Theme,
    schema: ThemeSchema,
    batch: list[ResourceInfo],
    when: datetime,
    prepared: Mapping[int, Any],
    options: Mapping[str, Any],
    report: ScanReport,
    root: RootConfig,
) -> None:
    """Ingest one batch; if it fails, each resource alone (see :func:`_ingest_pending`)."""
    if not batch:
        return
    try:
        _record(
            report,
            writer.run(partial(_ingest, ingester, schema, batch, when, prepared, options)),
            batch,
        )
    except Exception:
        for info in batch:
            try:
                _record(
                    report,
                    writer.run(partial(_ingest, ingester, schema, [info], when, prepared, options)),
                    [info],
                )
            except Exception as e:
                report.ingest_errors.append((info.relpath, f"{type(e).__name__}: {e}"))
                logger.warning("Ingest of %s in root %s failed: %s", info.relpath, root.id, e)


def _prepare(ingester: Theme, batch: list[ResourceInfo], root: RootConfig) -> Prepared:
    """Run the theme's ``prepare`` for some resources, on a reading thread (no database).
    If it raises, each resource is prepared alone; those that still fail are returned as
    errors and left out of the ingest, so they stay pending for the next scan."""
    try:
        return batch, dict(ingester.prepare(batch)), []
    except Exception:
        kept: list[ResourceInfo] = []
        prepared: dict[int, Any] = {}
        errors: list[tuple[str, str]] = []
        for info in batch:
            try:
                prepared.update(ingester.prepare([info]))
            except Exception as e:
                errors.append((info.relpath, f"{type(e).__name__}: {e}"))
                logger.warning("Reading %s in root %s failed: %s", info.relpath, root.id, e)
            else:
                kept.append(info)
        return kept, prepared, errors


def _gather(
    reading: list[Future[Prepared]], report: ScanReport
) -> tuple[list[ResourceInfo], dict[int, Any]]:
    """Wait for a batch's reading, and put its pieces back together in order. Errors are
    recorded here, on the scan's thread."""
    kept: list[ResourceInfo] = []
    prepared: dict[int, Any] = {}
    for future in reading:
        chunk, values, errors = future.result()
        kept.extend(chunk)
        prepared.update(values)
        report.ingest_errors.extend(errors)
    return kept, prepared


def _record(report: ScanReport, warnings: list[IngestWarning], batch: list[ResourceInfo]) -> None:
    report.ingested += len(batch)
    report.ingest_warnings.extend(warnings)


def _ingest(
    ingester: Theme,
    schema: ThemeSchema,
    batch: list[ResourceInfo],
    when: datetime,
    prepared: Mapping[int, Any],
    options: Mapping[str, Any],
    conn: Connection,
) -> list[IngestWarning]:
    """One ingest transaction: the theme's ingest, its flush, and marking the batch done."""
    ctx = IngestSession(conn, schema, prepared, options)
    ingester.ingest(batch, ctx)
    flushed = ctx.flush()
    conn.execute(
        update(Resource).where(Resource.id.in_([r.id for r in batch])).values(ingested_at=when)
    )
    return flushed.warnings
