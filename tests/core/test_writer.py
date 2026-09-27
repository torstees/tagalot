"""Tests for the single DB writer."""

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest
from sqlalchemy import Column, Connection, Integer, MetaData, Table, func, insert, select

from tagalot.core.db import create_keep_engine
from tagalot.core.keep import RootConfig
from tagalot.core.models import Base, Resource, ResourceKind, ResourceStatus
from tagalot.core.roots import sync_roots
from tagalot.core.scanner import (
    AppliedDiff,
    RootDiff,
    WalkEntry,
    apply_diff,
    diff_root,
    load_known,
    merge_applied,
    split_diff,
    walk_root,
)
from tagalot.core.writer import DbWriter, WriterClosedError

numbers = Table("numbers", MetaData(), Column("n", Integer, primary_key=True))


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "keep.db"
    engine = create_keep_engine(path)
    numbers.create(engine)
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture
def writer(db: Path) -> Iterator[DbWriter]:
    with DbWriter(create_keep_engine(db)) as w:
        yield w


def _add(n: int) -> Callable[[Connection], int]:
    def work(conn: Connection) -> int:
        conn.execute(insert(numbers).values(n=n))
        return n

    return work


def _count(db: Path) -> int:
    engine = create_keep_engine(db, read_only=True)
    try:
        with engine.connect() as conn:
            return conn.scalar(select(func.count()).select_from(numbers)) or 0
    finally:
        engine.dispose()


def test_jobs_run_on_the_writer_thread_and_return_results(writer: DbWriter) -> None:
    def work(conn: Connection) -> str:
        return threading.current_thread().name

    assert writer.run(work) == "db-writer"
    assert writer.submit(lambda conn: 42).result(5) == 42


def test_jobs_run_in_submission_order(writer: DbWriter, db: Path) -> None:
    order: list[int] = []

    def record(i: int, conn: Connection) -> None:
        order.append(i)

    futures = [writer.submit(partial(record, i)) for i in range(50)]
    for f in futures:
        f.result(5)
    assert order == list(range(50))


def test_failed_job_rolls_back_only_itself(writer: DbWriter, db: Path) -> None:
    def half_then_fail(conn: Connection) -> None:
        conn.execute(insert(numbers).values(n=2))
        raise ValueError("boom")

    first = writer.submit(_add(1))
    failed = writer.submit(half_then_fail)
    last = writer.submit(_add(3))
    assert first.result(5) == 1
    with pytest.raises(ValueError, match="boom"):
        failed.result(5)
    assert last.result(5) == 3
    engine = create_keep_engine(db, read_only=True)
    with engine.connect() as conn:
        assert set(conn.scalars(select(numbers.c.n))) == {1, 3}
    engine.dispose()


def test_many_threads_submitting(writer: DbWriter, db: Path) -> None:
    def submitter(base: int) -> None:
        for i in range(25):
            writer.submit(_add(base + i)).result(10)

    threads = [threading.Thread(target=submitter, args=(t * 1000,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _count(db) == 200


def test_cancelled_jobs_are_skipped(writer: DbWriter, db: Path) -> None:
    gate = threading.Event()
    blocker = writer.submit(lambda conn: gate.wait(5))
    skipped = writer.submit(_add(1))
    assert skipped.cancel()
    gate.set()
    blocker.result(5)
    writer.run(lambda conn: None)  # drain
    assert _count(db) == 0


def test_waiting_inside_a_job_is_refused(writer: DbWriter) -> None:
    def nested(conn: Connection) -> None:
        writer.run(lambda c: None)

    with pytest.raises(RuntimeError, match="deadlock"):
        writer.run(nested)


def test_close_finishes_queued_work_then_refuses_more(db: Path) -> None:
    writer = DbWriter(create_keep_engine(db))
    futures: list[Future[object]] = [writer.submit(_add(i)) for i in range(20)]
    writer.close()
    assert all(f.done() and f.exception() is None for f in futures)
    assert _count(db) == 20
    assert writer.closed
    with pytest.raises(WriterClosedError):
        writer.submit(lambda conn: None)
    writer.close()  # closing twice is harmless


# --- scans applied through the writer in short transactions ---


def test_split_diff_covers_everything_in_bounded_parts() -> None:

    entries = [WalkEntry(f"{i}", ResourceKind.FILE, "", 1, 1) for i in range(7)]
    diff = RootDiff(new=entries, missing=[1, 2, 3], unchanged=[9])
    parts = split_diff(diff, size=3)
    assert all(
        sum(len(getattr(p, n)) for n in ("new", "changed", "restored", "missing", "unchanged")) <= 3
        for p in parts
    )
    assert [e for p in parts for e in p.new] == entries
    assert [i for p in parts for i in p.missing] == [1, 2, 3]
    assert [i for p in parts for i in p.unchanged] == [9]
    assert split_diff(RootDiff()) == [RootDiff()]


def test_scan_through_the_writer_in_batches(writer: DbWriter, db: Path, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for i in range(1_234):
        (root / f"{i:04}.txt").write_bytes(b"x")
    when = datetime(2026, 9, 1, tzinfo=UTC)
    writer.run(lambda conn: sync_roots(conn, [RootConfig("r", "R", str(root))]))

    reader = create_keep_engine(db, read_only=True)
    with reader.connect() as conn:
        diff = diff_root(load_known(conn, "r"), walk_root(str(root)))
    parts = split_diff(diff, size=100)
    assert len(parts) == 13

    def apply(part: RootDiff, conn: Connection) -> AppliedDiff:
        return apply_diff(conn, "r", part, when)

    futures = [writer.submit(partial(apply, p)) for p in parts]
    applied = merge_applied(f.result(10) for f in futures)
    assert len(applied.new_ids) == 1_234
    with reader.connect() as conn:
        assert (
            conn.scalar(select(func.count()).where(Resource.status == ResourceStatus.OK)) == 1_234
        )
    reader.dispose()
