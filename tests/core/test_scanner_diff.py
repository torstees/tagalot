"""Tests for diffing a walk against stored resources and applying the result."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, insert, select, update

from tagalot.core.db import create_keep_engine
from tagalot.core.keep import RootConfig
from tagalot.core.models import (
    Base,
    Entity,
    EntityResource,
    Resource,
    ResourceKind,
    ResourceStatus,
    Root,
)
from tagalot.core.roots import RootCheck, record_root_check, sync_roots
from tagalot.core.scanner import (
    KnownResource,
    RootDiff,
    WalkEntry,
    apply_diff,
    diff_root,
    load_known,
    walk_root,
)

OK, OFFLINE, MISSING = ResourceStatus.OK, ResourceStatus.OFFLINE, ResourceStatus.MISSING
FILE, DIR = ResourceKind.FILE, ResourceKind.DIR
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _entry(
    relpath: str, size: int | None = 10, mtime: int = 100, kind: ResourceKind = FILE
) -> WalkEntry:
    return WalkEntry(relpath, kind, os.path.splitext(relpath)[1], size, mtime)


def _known(
    id: int, status: ResourceStatus = OK, size: int | None = 10, mtime: int = 100
) -> KnownResource:
    return KnownResource(id, FILE, size, mtime, status)


# --- the pure diff ---


@pytest.mark.parametrize(
    ("known", "walked", "expected"),
    [
        pytest.param({}, [_entry("a")], RootDiff(new=[_entry("a")]), id="new"),
        pytest.param({"a": _known(1)}, [_entry("a")], RootDiff(unchanged=[1]), id="unchanged"),
        pytest.param(
            {"a": _known(1)},
            [_entry("a", size=11)],
            RootDiff(changed=[(1, _entry("a", size=11))]),
            id="size",
        ),
        pytest.param(
            {"a": _known(1)},
            [_entry("a", mtime=101)],
            RootDiff(changed=[(1, _entry("a", mtime=101))]),
            id="mtime",
        ),
        pytest.param(
            {"a": _known(1)},
            [_entry("a", size=None, kind=DIR)],
            RootDiff(changed=[(1, _entry("a", size=None, kind=DIR))]),
            id="file became folder",
        ),
        pytest.param({"a": _known(1)}, [], RootDiff(missing=[1]), id="gone"),
        pytest.param({"a": _known(1, MISSING)}, [], RootDiff(), id="still gone"),
        pytest.param({"a": _known(1, OFFLINE)}, [], RootDiff(missing=[1]), id="offline, then gone"),
        pytest.param({"a": _known(1, MISSING)}, [_entry("a")], RootDiff(restored=[1]), id="back"),
        pytest.param(
            {"a": _known(1, OFFLINE)}, [_entry("a")], RootDiff(restored=[1]), id="online again"
        ),
        pytest.param(
            {"a": _known(1, MISSING)},
            [_entry("a", size=99)],
            RootDiff(changed=[(1, _entry("a", size=99))]),
            id="back but changed",
        ),
    ],
)
def test_diff(known: dict[str, KnownResource], walked: list[WalkEntry], expected: RootDiff) -> None:
    assert diff_root(known, walked) == expected


def test_paths_differing_only_in_case_are_different_resources() -> None:
    # A rename "song.flac" -> "Song.flac" is a new path plus a missing one; move detection
    # (fingerprints) reattaches links.
    diff = diff_root({"song.flac": _known(1)}, [_entry("Song.flac")])
    assert (diff.new, diff.missing) == ([_entry("Song.flac")], [1])


@pytest.mark.parametrize(
    ("unreadable", "missing"),
    [
        ([], [1, 2, 3]),
        (["Album"], [3]),  # everything under the unreadable folder is left alone
        (["Album/01.flac"], [2, 3]),  # one unreadable file
        (["Alb"], [1, 2, 3]),  # a name prefix is not a folder
        ([""], []),  # the root itself could not be read
    ],
)
def test_unreadable_paths_are_not_marked_missing(unreadable: list[str], missing: list[int]) -> None:
    known = {"Album/01.flac": _known(1), "Album/02.flac": _known(2), "loose.mp3": _known(3)}
    assert diff_root(known, [], unreadable).missing == missing


# --- scanning a real tree into a real database ---


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        sync_roots(conn, [RootConfig("r", "Root", str(tmp_path / "root"))])
    yield engine
    engine.dispose()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for rel in ["Album/01.flac", "Album/02.flac", "loose.mp3"]:
        (tmp_path / "root" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "root" / rel).write_bytes(b"audio")
    return tmp_path / "root"


def _scan(engine: Engine, root: Path, when: datetime, **walk: object) -> RootDiff:
    errors: list[str] = []
    entries = list(walk_root(str(root), on_error=lambda p, e: errors.append(p), **walk))  # type: ignore[arg-type]
    with engine.begin() as conn:
        diff = diff_root(load_known(conn, "r"), entries, errors)
        apply_diff(conn, "r", diff, when)
    return diff


def _rows(engine: Engine) -> dict[str, tuple[ResourceStatus, bytes | None, datetime]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(Resource.relpath, Resource.status, Resource.fingerprint, Resource.last_seen_at)
        )
        return {p: (st, fp, seen) for p, st, fp, seen in rows}


def test_first_scan_inserts_everything(engine: Engine, root: Path) -> None:
    diff = _scan(engine, root, T0)
    assert len(diff.new) == 3
    rows = _rows(engine)
    assert set(rows) == {"Album/01.flac", "Album/02.flac", "loose.mp3"}
    assert {st for st, _, seen in rows.values()} == {OK}
    with engine.connect() as conn:
        assert conn.scalar(select(Root.last_scan_at)) == T0
        assert set(conn.scalars(select(Resource.first_seen_at))) == {T0}


def test_apply_returns_new_ids(engine: Engine, root: Path) -> None:
    entries = list(walk_root(str(root)))
    with engine.begin() as conn:
        applied = apply_diff(conn, "r", diff_root({}, entries), T0)
        stored = {p: i for p, i in conn.execute(select(Resource.relpath, Resource.id))}
    assert applied.new_ids == stored


def test_rescan_without_changes_only_updates_last_seen(engine: Engine, root: Path) -> None:
    _scan(engine, root, T0)
    diff = _scan(engine, root, T0 + timedelta(hours=1))
    assert diff.is_empty
    assert len(diff.unchanged) == 3
    assert {seen for _, _, seen in _rows(engine).values()} == {T0 + timedelta(hours=1)}


def test_changed_file_clears_fingerprint(engine: Engine, root: Path) -> None:
    _scan(engine, root, T0)
    with engine.begin() as conn:
        conn.execute(update(Resource).values(fingerprint=b"old"))
    (root / "Album/01.flac").write_bytes(b"re-encoded audio")
    os.utime(root / "Album/01.flac", ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))

    diff = _scan(engine, root, T0 + timedelta(hours=1))
    assert [e.relpath for _, e in diff.changed] == ["Album/01.flac"]
    rows = _rows(engine)
    assert rows["Album/01.flac"][1] is None
    assert rows["Album/02.flac"][1] == b"old"
    with engine.connect() as conn:
        size, mtime = conn.execute(
            select(Resource.size, Resource.mtime_ns).where(Resource.relpath == "Album/01.flac")
        ).one()
    assert (size, mtime) == (len(b"re-encoded audio"), 2_000_000_000_000_000_000)


def test_deleted_file_is_marked_missing_and_links_survive(engine: Engine, root: Path) -> None:
    _scan(engine, root, T0)
    with engine.begin() as conn:
        rid = conn.scalar(select(Resource.id).where(Resource.relpath == "loose.mp3"))
        conn.execute(insert(Entity).values(id=1, type="generic.file", title="Loose"))
        conn.execute(insert(EntityResource).values(entity_id=1, resource_id=rid, role="file"))
    (root / "loose.mp3").unlink()

    _scan(engine, root, T0 + timedelta(hours=1))
    status, _, seen = _rows(engine)["loose.mp3"]
    assert (status, seen) == (MISSING, T0)  # last seen at the first scan
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(EntityResource)) == 1


def test_file_that_returns_keeps_its_id(engine: Engine, root: Path) -> None:
    _scan(engine, root, T0)
    data = (root / "loose.mp3").read_bytes()
    stat = (root / "loose.mp3").stat()
    with engine.connect() as conn:
        rid = conn.scalar(select(Resource.id).where(Resource.relpath == "loose.mp3"))
    (root / "loose.mp3").unlink()
    _scan(engine, root, T0 + timedelta(hours=1))

    (root / "loose.mp3").write_bytes(data)
    os.utime(root / "loose.mp3", ns=(stat.st_atime_ns, stat.st_mtime_ns))
    diff = _scan(engine, root, T0 + timedelta(hours=2))
    assert diff.restored == [rid]
    assert _rows(engine)["loose.mp3"][0] == OK


def test_offline_root_comes_back(engine: Engine, root: Path) -> None:
    _scan(engine, root, T0)
    with engine.begin() as conn:
        record_root_check(conn, "r", RootCheck(online=False, error="unplugged"))
    assert {st for st, _, _ in _rows(engine).values()} == {OFFLINE}

    diff = _scan(engine, root, T0 + timedelta(days=1))
    assert len(diff.restored) == 3
    assert {st for st, _, _ in _rows(engine).values()} == {OK}


def test_unreadable_folder_keeps_its_files(
    engine: Engine, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scan(engine, root, T0)
    real_scandir = os.scandir

    def flaky(path: str) -> "os._ScandirIterator[str]":
        if Path(path).name == "Album":
            raise PermissionError(13, "Access is denied", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky)
    diff = _scan(engine, root, T0 + timedelta(hours=1))
    assert diff.missing == []
    rows = _rows(engine)
    assert rows["Album/01.flac"] == (OK, None, T0)  # not seen, so last_seen_at doesn't move
    assert rows["loose.mp3"][2] == T0 + timedelta(hours=1)


def test_directories_are_tracked_when_requested(engine: Engine, root: Path) -> None:
    _scan(engine, root, T0, dirs=True)
    with engine.connect() as conn:
        kinds = {p: k for p, k in conn.execute(select(Resource.relpath, Resource.kind))}
    assert kinds["Album"] is DIR
    assert kinds["Album/01.flac"] is FILE


def test_large_batches(engine: Engine, root: Path) -> None:
    for i in range(1_234):  # more than two insert batches
        (root / f"bulk/{i:04}.txt").parent.mkdir(exist_ok=True)
        (root / f"bulk/{i:04}.txt").write_bytes(b"x")
    diff = _scan(engine, root, T0)
    assert len(diff.new) == 1_237
    for p in (root / "bulk").iterdir():
        p.unlink()
    diff = _scan(engine, root, T0 + timedelta(hours=1))
    assert len(diff.missing) == 1_234
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).where(Resource.status == MISSING)) == 1_234
