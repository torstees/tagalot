"""Tests for root reachability and root status recording."""

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, insert, select

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
from tagalot.core.roots import RootCheck, check_root, record_root_check, sync_roots

# --- checking reachability (file system only) ---


def test_existing_folder_is_online(tmp_path: Path) -> None:
    assert check_root(str(tmp_path)) == RootCheck(online=True)


def test_empty_folder_is_online(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert check_root(str(tmp_path / "empty")).online


def test_missing_path_is_offline(tmp_path: Path) -> None:
    # AGENTS.md: simulate an offline root by pointing it at a nonexistent path.
    check = check_root(str(tmp_path / "unplugged-drive"))
    assert not check.online
    assert check.error is not None
    assert "was not found" in check.error


def test_file_is_offline(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    check = check_root(str(tmp_path / "a.txt"))
    assert (check.online, check.error) == (False, f"{tmp_path / 'a.txt'} is not a folder")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (PermissionError(13, "Access is denied"), "Permission denied reading"),
        (OSError(64, "The specified network name is no longer available"), "no longer available"),
    ],
)
def test_os_errors_are_offline(error: OSError, message: str) -> None:
    def probe(path: str) -> None:
        raise error

    check = check_root(r"\\nas\music", probe=probe)
    assert not check.online
    assert message in (check.error or "")


def test_unresponsive_share_times_out() -> None:
    release = threading.Event()

    def hang(path: str) -> None:
        release.wait(10)

    start = time.monotonic()
    check = check_root(r"\\sleeping-nas\music", timeout=0.2, probe=hang)
    elapsed = time.monotonic() - start
    release.set()
    assert not check.online
    assert check.error == r"No response from \\sleeping-nas\music after 0.2 s"
    assert elapsed < 2


def test_unexpected_errors_are_not_swallowed() -> None:
    def probe(path: str) -> None:
        raise ValueError("bug")

    with pytest.raises(ValueError, match="bug"):
        check_root("/x", probe=probe)


# --- recording results (database) ---


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        sync_roots(conn, [RootConfig("nas", "NAS", r"\\nas\music"), RootConfig("local", "L", "/l")])
        # Every row lists the same keys: a multi-row insert takes its columns from the first row.
        ok, missing = ResourceStatus.OK, ResourceStatus.MISSING
        conn.execute(
            insert(Resource),
            [
                {"root_id": root, "relpath": rel, "kind": ResourceKind.FILE, "status": status}
                for root, rel, status in [
                    ("nas", "a.flac", ok),
                    ("nas", "b.flac", ok),
                    ("nas", "gone.flac", missing),
                    ("local", "c.txt", ok),
                ]
            ],
        )
        conn.execute(insert(Entity).values(id=1, type="generic.file", title="A"))
        conn.execute(insert(EntityResource).values(entity_id=1, resource_id=1, role="file"))
    yield engine
    engine.dispose()


def _statuses(engine: Engine) -> dict[str, ResourceStatus]:
    with engine.connect() as conn:
        return {p: st for p, st in conn.execute(select(Resource.relpath, Resource.status))}


def test_sync_roots_adds_and_renames_but_never_removes(engine: Engine) -> None:
    with engine.begin() as conn:
        sync_roots(
            conn, [RootConfig("nas", "NAS share (renamed)", "x"), RootConfig("new", "N", "y")]
        )
    with engine.connect() as conn:
        rows = {i: n for i, n in conn.execute(select(Root.id, Root.name))}
    assert rows == {"nas": "NAS share (renamed)", "local": "L", "new": "N"}


def test_offline_root_marks_ok_resources_offline(engine: Engine) -> None:
    with engine.begin() as conn:
        marked = record_root_check(conn, "nas", RootCheck(online=False, error="timed out"))
    assert marked == 2
    assert _statuses(engine) == {
        "a.flac": ResourceStatus.OFFLINE,
        "b.flac": ResourceStatus.OFFLINE,
        "gone.flac": ResourceStatus.MISSING,  # still missing: that is what we last knew
        "c.txt": ResourceStatus.OK,  # other roots are untouched
    }
    with engine.connect() as conn:
        root = conn.execute(select(Root.online, Root.last_error).where(Root.id == "nas")).one()
    assert tuple(root) == (False, "timed out")


def test_offline_root_never_deletes_anything(engine: Engine) -> None:
    def counts() -> tuple[int, int, int]:
        with engine.connect() as conn:
            return tuple(
                conn.scalar(select(func.count()).select_from(m)) or 0
                for m in (Resource, Entity, EntityResource)
            )  # type: ignore[return-value]

    before = counts()
    for _ in range(3):  # repeated offline scans
        with engine.begin() as conn:
            record_root_check(conn, "nas", RootCheck(online=False, error="unplugged"))
    assert counts() == before


def test_online_root_clears_error_and_leaves_statuses_for_the_scan(engine: Engine) -> None:
    with engine.begin() as conn:
        record_root_check(conn, "nas", RootCheck(online=False, error="timed out"))
        assert record_root_check(conn, "nas", RootCheck(online=True)) == 0
    with engine.connect() as conn:
        root = conn.execute(select(Root.online, Root.last_error).where(Root.id == "nas")).one()
    assert tuple(root) == (True, None)
    assert _statuses(engine)["a.flac"] == ResourceStatus.OFFLINE  # the scan will set ok/missing
