"""Tests for file fingerprints and the fingerprint queue."""

import hashlib
import ntpath
import os
import posixpath
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, select, update

from tagalot.core.db import create_keep_engine
from tagalot.core.fingerprint import (
    CHUNK,
    DIGEST_SIZE,
    FingerprintJob,
    FingerprintResult,
    compute_fingerprints,
    fingerprint_file,
    pending_fingerprints,
    store_fingerprints,
)
from tagalot.core.keep import RootConfig
from tagalot.core.models import Base, Resource, ResourceKind, ResourceStatus
from tagalot.core.roots import local_path, sync_roots


def _expected(data: bytes) -> bytes:
    h = hashlib.blake2b(len(data).to_bytes(8, "little"), digest_size=DIGEST_SIZE)
    h.update(data if len(data) <= 2 * CHUNK else data[:CHUNK] + data[-CHUNK:])
    return h.digest()


def _write(path: Path, data: bytes) -> str:
    path.write_bytes(data)
    return str(path)


# --- the fingerprint itself ---


@pytest.mark.parametrize(
    "size",
    [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 2 * CHUNK, 2 * CHUNK + 1, 1024 * 1024 + 7],
)
def test_matches_the_design_formula(tmp_path: Path, size: int) -> None:
    data = bytes((i * 7 + 3) % 251 for i in range(size))
    assert fingerprint_file(_write(tmp_path / "f", data)) == _expected(data)
    assert len(fingerprint_file(str(tmp_path / "f"))) == DIGEST_SIZE


def test_what_the_fingerprint_notices(tmp_path: Path) -> None:
    base = bytearray(os.urandom(4 * CHUNK))
    fp = fingerprint_file(_write(tmp_path / "base", bytes(base)))

    head, tail, middle = bytearray(base), bytearray(base), bytearray(base)
    head[10] ^= 1
    tail[-10] ^= 1
    middle[2 * CHUNK] ^= 1
    assert fingerprint_file(_write(tmp_path / "head", bytes(head))) != fp
    assert fingerprint_file(_write(tmp_path / "tail", bytes(tail))) != fp
    assert fingerprint_file(_write(tmp_path / "longer", bytes(base) + b"x")) != fp
    # Documented limitation (DESIGN.md §5): the middle isn't read; dedupe offers a full hash.
    assert fingerprint_file(_write(tmp_path / "middle", bytes(middle))) == fp


def test_same_content_same_fingerprint(tmp_path: Path) -> None:
    data = os.urandom(300_000)
    a = fingerprint_file(_write(tmp_path / "a.flac", data))
    b = fingerprint_file(_write(tmp_path / "moved and renamed.flac", data))
    assert a == b


# --- local paths (AGENTS.md rule 6) ---


@pytest.mark.parametrize(
    ("pathmod", "root", "relpath", "expected"),
    [
        (ntpath, r"\\nas\music", "Artist/Album/01.flac", r"\\nas\music\Artist\Album\01.flac"),
        (ntpath, "M:\\Music\\", "a.flac", r"M:\Music\a.flac"),
        (ntpath, r"\\?\UNC\nas\music", "a b/c.flac", r"\\?\UNC\nas\music\a b\c.flac"),
        (posixpath, "/Volumes/music", "Artist/01.flac", "/Volumes/music/Artist/01.flac"),
        (posixpath, "/mnt/nas/", "Björk/Hunter.flac", "/mnt/nas/Björk/Hunter.flac"),
    ],
)
def test_local_path(pathmod: object, root: str, relpath: str, expected: str) -> None:
    assert local_path(root, relpath, pathmod) == expected  # type: ignore[arg-type]


# --- queue, workers, and storing ---


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        sync_roots(conn, [RootConfig("r", "R", str(tmp_path / "root")), RootConfig("o", "O", "/o")])
    yield engine
    engine.dispose()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "root").mkdir()
    return tmp_path / "root"


def _add(engine: Engine, root: Path, relpath: str, data: bytes = b"", **values: object) -> int:
    path = root / relpath
    if values.get("kind") != ResourceKind.DIR:
        path.write_bytes(data)
    st = path.stat() if path.exists() else None
    row = {
        "root_id": "r",
        "relpath": relpath,
        "kind": ResourceKind.FILE,
        "status": ResourceStatus.OK,
        "size": st.st_size if st else None,
        "mtime_ns": st.st_mtime_ns if st else None,
        "fingerprint": None,
    } | values
    with engine.begin() as conn:
        return int(conn.execute(insert(Resource).values(**row).returning(Resource.id)).scalar_one())


def test_pending_lists_only_ok_files_without_fingerprints_newest_first(
    engine: Engine, root: Path
) -> None:
    older = _add(engine, root, "older.flac", b"a")
    newer = _add(engine, root, "newer.flac", b"b")
    _add(engine, root, "done.flac", b"c", fingerprint=b"x" * 16)
    _add(engine, root, "gone.flac", b"d", status=ResourceStatus.MISSING)
    _add(engine, root, "away.flac", b"e", status=ResourceStatus.OFFLINE)
    _add(engine, root, "Album", kind=ResourceKind.DIR)
    _add(engine, root, "elsewhere.flac", b"f", root_id="o")
    with engine.connect() as conn:
        assert [j.resource_id for j in pending_fingerprints(conn, root_id="r")] == [newer, older]
        assert [j.resource_id for j in pending_fingerprints(conn, root_id="r", limit=1)] == [newer]
        assert len(pending_fingerprints(conn)) == 3  # all roots


def test_compute_then_store(engine: Engine, root: Path) -> None:
    data = os.urandom(200_000)
    rid = _add(engine, root, "a.flac", data)
    with engine.connect() as conn:
        jobs = pending_fingerprints(conn)
    results = list(compute_fingerprints(jobs, lambda j: local_path(str(root), j.relpath)))
    with engine.begin() as conn:
        assert store_fingerprints(conn, results) == 1
        stored = conn.scalar(select(Resource.fingerprint).where(Resource.id == rid))
    assert stored == _expected(data)
    with engine.connect() as conn:
        assert pending_fingerprints(conn) == []


def test_files_changed_since_queued_are_skipped(engine: Engine, root: Path) -> None:
    _add(engine, root, "a.flac", b"original")
    with engine.connect() as conn:
        jobs = pending_fingerprints(conn)
    (root / "a.flac").write_bytes(b"edited after queueing")
    assert list(compute_fingerprints(jobs, lambda j: local_path(str(root), j.relpath))) == []


def test_unreadable_files_are_reported_and_the_rest_continue(engine: Engine, root: Path) -> None:
    _add(engine, root, "gone.flac", b"a")
    kept = _add(engine, root, "kept.flac", b"b")
    (root / "gone.flac").unlink()
    with engine.connect() as conn:
        jobs = pending_fingerprints(conn)
    errors: list[tuple[str, type[OSError]]] = []
    results = list(
        compute_fingerprints(
            jobs,
            lambda j: local_path(str(root), j.relpath),
            on_error=lambda j, e: errors.append((j.relpath, type(e))),
        )
    )
    assert [r.resource_id for r in results] == [kept]
    assert errors == [("gone.flac", FileNotFoundError)]


def _jobs(root: Path, count: int) -> list[FingerprintJob]:
    """``count`` small files, every third one missing (unreadable)."""
    jobs = []
    for n in range(count):
        path = root / f"f{n}.bin"
        path.write_bytes(os.urandom(100 + n))
        st = path.stat()
        jobs.append(FingerprintJob(n, "r", path.name, st.st_size, st.st_mtime_ns))
        if n % 3 == 2:
            path.unlink()
    return jobs


def test_several_threads_give_the_same_results_in_order(root: Path) -> None:
    jobs = _jobs(root, 100)
    errors: dict[int, list[tuple[int, int]]] = {1: [], 8: []}

    def outcome(threads: int) -> list[FingerprintResult]:
        return list(
            compute_fingerprints(
                jobs,
                lambda j: str(root / j.relpath),
                on_error=lambda j, e: errors[threads].append(
                    (j.resource_id, threading.get_ident())
                ),
                threads=threads,
            )
        )

    alone, together = outcome(1), outcome(8)
    assert together == alone
    assert [r.resource_id for r in together] == [n for n in range(100) if n % 3 != 2]
    assert together[0].fingerprint == _expected((root / "f0.bin").read_bytes())
    # Errors are reported in order, from the thread iterating (never a hashing thread).
    assert [n for n, _ in errors[8]] == [n for n in range(100) if n % 3 == 2]
    assert {ident for _, ident in errors[8]} == {threading.get_ident()}


def test_stopping_early_leaves_no_threads(root: Path) -> None:
    results = compute_fingerprints(_jobs(root, 200), lambda j: str(root / j.relpath), threads=4)
    assert [next(results).resource_id for _ in range(3)] == [0, 1, 3]
    results.close()
    assert not [t for t in threading.enumerate() if t.name.startswith("fingerprint")]


def test_store_never_attaches_to_a_newer_version(engine: Engine, root: Path) -> None:
    rid = _add(engine, root, "a.flac", b"v1")
    stale = FingerprintResult(rid, size=2, mtime_ns=1, fingerprint=b"f" * 16)
    with engine.begin() as conn:
        conn.execute(update(Resource).values(size=99, mtime_ns=2))  # a later scan saw a change
        assert store_fingerprints(conn, [stale]) == 0
        assert conn.scalar(select(Resource.fingerprint)) is None


def test_storing_nothing(engine: Engine) -> None:
    with engine.begin() as conn:
        assert store_fingerprints(conn, []) == 0


def test_job_fields() -> None:
    job = FingerprintJob(1, "r", "a/b.flac", 10, 20)
    assert (job.root_id, job.relpath) == ("r", "a/b.flac")
