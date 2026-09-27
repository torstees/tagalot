"""End-to-end scans through scan_root() with a real keep database (issue #25)."""

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import ColumnElement, Connection, Engine, func, insert, select

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import (
    Entity,
    EntityResource,
    EntityTag,
    Resource,
    ResourceKind,
    ResourceStatus,
    Root,
    Tag,
)
from tagalot.core.roots import check_root
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.writer import DbWriter

T0 = datetime(2026, 9, 1, tzinfo=UTC)
OK, OFFLINE, MISSING = ResourceStatus.OK, ResourceStatus.OFFLINE, ResourceStatus.MISSING


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    files: Path

    def scan(
        self,
        root: RootConfig | None = None,
        path: str | None = None,
        when: datetime = T0,
        extensions: set[str] | None = None,
        dirs: bool = False,
    ) -> ScanReport:
        root = root or RootConfig("r", "Music", str(self.files))
        return scan_root(
            self.writer,
            self.reader,
            root,
            path or root.path,
            when=when,
            extensions=extensions,
            dirs=dirs,
        )

    def statuses(self) -> dict[str, ResourceStatus]:
        with self.reader.connect() as conn:
            return {p: s for p, s in conn.execute(select(Resource.relpath, Resource.status))}

    def count(self, *where: ColumnElement[bool]) -> int:
        with self.reader.connect() as conn:
            return conn.scalar(select(func.count()).select_from(Resource).where(*where)) or 0


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("generic", 1)))
    files = tmp_path / "files"
    for rel in ["Album/01.flac", "Album/02.flac", "Album/cover.jpg", "notes.txt"]:
        (files / rel).parent.mkdir(parents=True, exist_ok=True)
        (files / rel).write_bytes(os.urandom(1000 + len(rel)))
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        yield Env(writer, reader, files)
    reader.dispose()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_first_scan(env: Env) -> None:
    report = env.scan()
    assert (report.online, report.new, report.missing) == (True, 4, 0)
    assert report.fingerprinted == 4
    assert env.count(Resource.fingerprint.is_(None)) == 0
    assert set(env.statuses().values()) == {OK}
    with env.reader.connect() as conn:
        assert conn.execute(select(Root.online, Root.last_scan_at)).one() == (True, T0)


def test_rescan_without_changes(env: Env) -> None:
    env.scan()
    report = env.scan(when=T0 + timedelta(hours=1))
    assert (report.new, report.changed, report.missing, report.unchanged) == (0, 0, 0, 4)
    assert report.fingerprinted == 0
    assert report.moves == []


def test_new_changed_and_missing(env: Env) -> None:
    env.scan()
    _write(env.files / "Album/03.flac", b"new track")
    _write(env.files / "notes.txt", b"edited notes, now a different size")
    (env.files / "Album/02.flac").unlink()

    report = env.scan(when=T0 + timedelta(hours=1))
    assert (report.new, report.changed, report.missing, report.unchanged) == (1, 1, 1, 2)
    assert report.fingerprinted == 2  # the new file and the changed one
    assert env.statuses() == {
        "Album/01.flac": OK,
        "Album/02.flac": MISSING,
        "Album/03.flac": OK,
        "Album/cover.jpg": OK,
        "notes.txt": OK,
    }


def test_offline_root_never_deletes_and_comes_back(env: Env, tmp_path: Path) -> None:
    env.scan()
    root = RootConfig("r", "Music", str(env.files))
    unplugged = str(tmp_path / "unplugged")  # AGENTS.md: offline = a nonexistent path

    report = env.scan(root, path=unplugged, when=T0 + timedelta(days=1))
    assert (report.online, report.new, report.missing) == (False, 0, 0)
    assert report.error is not None
    assert "not found" in report.error
    assert env.count() == 4
    assert set(env.statuses().values()) == {OFFLINE}

    report = env.scan(root, when=T0 + timedelta(days=2))
    assert (report.online, report.restored, report.missing) == (True, 4, 0)
    assert set(env.statuses().values()) == {OK}


def test_moved_folder_keeps_links_and_tags(env: Env) -> None:
    env.scan()

    def tag_song(conn: Connection) -> None:
        rid = conn.scalar(select(Resource.id).where(Resource.relpath == "Album/01.flac"))
        conn.execute(insert(Entity).values(id=1, type="generic.file", title="Intro"))
        conn.execute(insert(EntityResource).values(entity_id=1, resource_id=rid, role="file"))
        conn.execute(insert(Tag).values(id=1, name="Favorites"))
        conn.execute(insert(EntityTag).values(entity_id=1, tag_id=1))

    env.writer.run(tag_song)
    (env.files / "Album").rename(env.files / "Album (2024 Remaster)")

    report = env.scan(when=T0 + timedelta(days=1))
    assert sorted((m.old_path, m.new_path) for m in report.moves) == [
        ("r:Album/01.flac", "r:Album (2024 Remaster)/01.flac"),
        ("r:Album/02.flac", "r:Album (2024 Remaster)/02.flac"),
        ("r:Album/cover.jpg", "r:Album (2024 Remaster)/cover.jpg"),
    ]
    assert MISSING not in env.statuses().values()
    with env.reader.connect() as conn:
        linked = conn.scalar(
            select(Resource.relpath).join(EntityResource).where(EntityResource.entity_id == 1)
        )
        assert linked == "Album (2024 Remaster)/01.flac"
        assert conn.scalar(select(func.count()).select_from(EntityTag)) == 1


def test_scan_options_are_applied(env: Env) -> None:
    _write(env.files / "Album/.DS_Store", b"junk")
    root = RootConfig("r", "Music", str(env.files), exclude=["**/.DS_Store"])
    env.scan(root, extensions={".flac", ".jpg"}, dirs=True)
    assert env.statuses().keys() == {"Album", "Album/01.flac", "Album/02.flac", "Album/cover.jpg"}
    assert env.count(Resource.kind == ResourceKind.DIR) == 1


def test_read_errors_are_reported_and_files_kept(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.scan()
    real_scandir = os.scandir

    def flaky(path: str) -> "os._ScandirIterator[str]":
        if Path(path).name == "Album":
            raise PermissionError(13, "Access is denied", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky)
    report = env.scan(when=T0 + timedelta(hours=1))
    assert [p for p, _ in report.read_errors] == ["Album"]
    assert report.missing == 0
    assert set(env.statuses().values()) == {OK}


def test_large_scan_uses_short_transactions(env: Env) -> None:
    for i in range(1_234):
        _write(env.files / f"bulk/{i:04}.txt", i.to_bytes(4, "little") * 10)
    report = env.scan()
    assert report.new == 1_238
    assert report.fingerprinted == 1_238
    assert env.count(Resource.status == OK, Resource.fingerprint.is_not(None)) == 1_238


# --- Windows-style roots (AGENTS.md rule 6) ---


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path forms")
def test_extended_length_windows_root(env: Env) -> None:
    long_form = "\\\\?\\" + str(env.files.resolve())
    report = env.scan(RootConfig("r", "Music", long_form))
    assert report.online
    assert report.new == 4
    assert "Album/01.flac" in env.statuses()  # relpaths stay POSIX, whatever the root form


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path forms")
def test_unc_root_via_admin_share(env: Env) -> None:
    resolved = env.files.resolve()
    unc = f"\\\\localhost\\{resolved.drive[0]}$" + str(resolved)[2:]
    if not check_root(unc, timeout=5).online:
        pytest.skip("the \\\\localhost admin share is not available here")
    report = env.scan(RootConfig("r", "Music", unc))
    assert report.online
    assert report.new == 4
    assert report.fingerprinted == 4
    assert all("\\" not in p for p in env.statuses())
