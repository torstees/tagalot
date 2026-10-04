"""File fingerprints: blake2b over size, head, and tail (DESIGN.md §5).

Hashing reads only the first and last 64 KiB, so it is cheap even over a network share.
:func:`compute_fingerprints` does the file I/O and runs in workers; :func:`store_fingerprints`
is called by the DB writer.

On a share most of the time is latency (opening a file takes milliseconds), so files are
hashed several at a time (#273, DESIGN.md §3 "Performance").
"""

import hashlib
import logging
import os
from collections import deque
from collections.abc import Callable, Generator, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy import Connection, bindparam, select, update

from tagalot.core.models import Resource, ResourceKind, ResourceStatus

logger = logging.getLogger(__name__)

CHUNK = 64 * 1024
THREADS = 8
"""Files hashed at once. Reading releases the GIL, and a share's latency overlaps."""
DIGEST_SIZE = 16
"""128 bits: ample for move detection and duplicate candidates, and a compact index."""


def fingerprint_file(path: str) -> bytes:
    """Return ``blake2b(size || first 64 KiB || last 64 KiB)`` for a file.

    ``size`` is 8 bytes little-endian. Files of at most 128 KiB are hashed whole, once.
    Raises ``OSError`` if the file can't be read.
    """
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        h = hashlib.blake2b(size.to_bytes(8, "little"), digest_size=DIGEST_SIZE)
        if size <= 2 * CHUNK:
            h.update(f.read())
        else:
            h.update(f.read(CHUNK))
            f.seek(-CHUNK, os.SEEK_END)
            h.update(f.read(CHUNK))
    return h.digest()


@dataclass(frozen=True)
class FingerprintJob:
    """A resource that needs a fingerprint, as it was when queued."""

    resource_id: int
    root_id: str
    relpath: str
    size: int | None
    mtime_ns: int | None


@dataclass(frozen=True)
class FingerprintResult:
    resource_id: int
    size: int | None
    mtime_ns: int | None
    fingerprint: bytes


def pending_fingerprints(
    conn: Connection, *, root_id: str | None = None, limit: int | None = 500
) -> list[FingerprintJob]:
    """Files that are ``ok`` and have no fingerprint, newest first (move detection needs new
    files first). ``limit=None`` returns them all."""
    query = (
        select(Resource.id, Resource.root_id, Resource.relpath, Resource.size, Resource.mtime_ns)
        .where(
            Resource.kind == ResourceKind.FILE,
            Resource.status == ResourceStatus.OK,
            Resource.fingerprint.is_(None),
            Resource.parent_resource_id.is_(None),
        )
        .order_by(Resource.id.desc())
        .limit(limit)  # None means no limit
    )
    if root_id is not None:
        query = query.where(Resource.root_id == root_id)
    return [FingerprintJob(*row) for row in conn.execute(query)]


def compute_fingerprints(
    jobs: Iterable[FingerprintJob],
    local_path: Callable[[FingerprintJob], str],
    on_error: Callable[[FingerprintJob, OSError], None] | None = None,
    *,
    threads: int | None = None,
) -> Generator[FingerprintResult]:
    """Hash each job's file, ``threads`` (default :data:`THREADS`) at a time. Runs in a
    worker; never touches the database. Results come in the jobs' order, and ``on_error``
    is called from the thread iterating, never from a hashing thread.

    Files whose size or mtime no longer match the job are skipped: the next scan will record
    the change. Unreadable files go to ``on_error`` and the rest continue.
    """

    def hash_one(job: FingerprintJob) -> FingerprintResult | OSError | None:
        path = local_path(job)
        try:
            st = os.stat(path)
            if (st.st_size, st.st_mtime_ns) != (job.size, job.mtime_ns):
                logger.debug("Skipping %s: changed since it was queued", path)
                return None
            return FingerprintResult(
                job.resource_id, job.size, job.mtime_ns, fingerprint_file(path)
            )
        except OSError as e:
            return e

    def report(
        job: FingerprintJob, outcome: FingerprintResult | OSError | None
    ) -> Iterator[FingerprintResult]:
        if isinstance(outcome, FingerprintResult):
            yield outcome
        elif isinstance(outcome, OSError):
            if on_error is None:
                logger.warning("Cannot fingerprint %s: %s", local_path(job), outcome)
            else:
                on_error(job, outcome)

    threads = THREADS if threads is None else threads
    if threads <= 1:
        for job in jobs:
            yield from report(job, hash_one(job))
        return
    # A window of a few jobs per thread keeps every thread busy without queueing them all,
    # so stopping early (the consumer closing this generator) leaves little to cancel.
    pool = ThreadPoolExecutor(threads, thread_name_prefix="fingerprint")
    window: deque[tuple[FingerprintJob, Future[FingerprintResult | OSError | None]]] = deque()
    try:
        for job in jobs:
            window.append((job, pool.submit(hash_one, job)))
            if len(window) >= threads * 4:
                done, future = window.popleft()
                yield from report(done, future.result())
        while window:
            done, future = window.popleft()
            yield from report(done, future.result())
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def store_fingerprints(conn: Connection, results: Iterable[FingerprintResult]) -> int:
    """Save fingerprints. Called by the DB writer.

    A row is only updated if its size and mtime still match what was hashed, so a result can
    never attach to a newer version of the file. Returns the number of rows updated.
    """
    params = [
        {"b_id": r.resource_id, "b_size": r.size, "b_mtime": r.mtime_ns, "b_fp": r.fingerprint}
        for r in results
    ]
    if not params:
        return 0
    result = conn.execute(
        update(Resource)
        .where(
            # bindparam names must differ from column names in an UPDATE.
            Resource.id == bindparam("b_id"),
            Resource.size == bindparam("b_size"),
            Resource.mtime_ns == bindparam("b_mtime"),
        )
        .values(fingerprint=bindparam("b_fp")),
        params,
    )
    return result.rowcount
