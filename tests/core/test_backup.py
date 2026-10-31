"""``tagalot --backup`` (#608): checked, dated copies of a keep, and pruning."""

import shutil
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, select

from tagalot.__main__ import main
from tagalot.core import backup
from tagalot.core.backup import BackupError, backup_keep, check_database, good_backups
from tagalot.core.db import open_keep_database
from tagalot.core.keep import KEEP_DB, KEEP_TOML, ThemeRef, create_keep, open_keep
from tagalot.core.models import Tag
from tagalot.core.tags import add_tag

T0 = datetime(2026, 10, 30, 3, 0)


@pytest.fixture
def keep(tmp_path: Path) -> Iterator[tuple[Path, Engine]]:
    """A keep with one tag, its database left open (in WAL) as the app's would be."""
    made, engine = open_keep_database(
        create_keep(tmp_path / "Movies.keep", "M", ThemeRef("generic", 1))
    )
    with engine.begin() as conn:
        add_tag(conn, None, "Favorites")
    yield made.dir, engine
    engine.dispose()


def tags_in(db: Path) -> list[str]:
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    try:
        with engine.connect() as conn:
            return list(conn.scalars(select(Tag.name)))
    finally:
        engine.dispose()


def test_a_backup_is_a_checked_dated_copy(keep: tuple[Path, Engine], tmp_path: Path) -> None:
    keep_dir, engine = keep
    with engine.begin() as conn:  # written while open, still only in the WAL
        add_tag(conn, None, "Watch later")
    result = backup_keep(keep_dir, tmp_path / "backups", now=lambda: T0)
    assert result.ok
    assert result.exit_code == 0
    assert result.folder == tmp_path / "backups" / "Movies" / "2026-10-30_0300"
    assert sorted(p.name for p in result.folder.iterdir()) == [KEEP_DB, KEEP_TOML]
    assert sorted(tags_in(result.folder / KEEP_DB)) == ["Favorites", "Watch later"]
    assert check_database(result.folder / KEEP_DB) == ""
    log = (tmp_path / "backups" / "backup.log").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1
    assert log[0].startswith("2026-10-30 03:00:00  ok  Movies.keep -> 2026-10-30_0300 (")


def test_restoring_is_copying_the_files_back(keep: tuple[Path, Engine], tmp_path: Path) -> None:
    keep_dir, _ = keep
    result = backup_keep(keep_dir, tmp_path / "backups", now=lambda: T0)
    assert result.folder is not None
    restored = tmp_path / "Restored.keep"
    shutil.copytree(result.folder, restored)
    _, engine = open_keep_database(open_keep(restored))
    try:
        with engine.connect() as conn:
            assert list(conn.scalars(select(Tag.name))) == ["Favorites"]
    finally:
        engine.dispose()


def test_keep_last_deletes_the_oldest(keep: tuple[Path, Engine], tmp_path: Path) -> None:
    keep_dir, _ = keep
    to = tmp_path / "backups"
    times = [T0 + timedelta(days=d) for d in range(4)]
    clock = iter(times)
    results = [backup_keep(keep_dir, to, keep_last=2, now=clock.__next__) for _ in times]
    assert [p.name for p in good_backups(to / "Movies")] == ["2026-11-01_0300", "2026-11-02_0300"]
    assert [len(r.deleted) for r in results] == [0, 0, 1, 1]
    log = (to / "backup.log").read_text(encoding="utf-8").splitlines()
    assert len(log) == 4
    assert log[-1].endswith(", deleted 1 older")


def test_a_damaged_copy_fails_and_older_ones_stay(
    keep: tuple[Path, Engine], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep_dir, _ = keep
    to = tmp_path / "backups"
    first = backup_keep(keep_dir, to, keep_last=1, now=lambda: T0)
    monkeypatch.setattr(backup, "check_database", lambda path: "page 7: btree damaged")
    later = backup_keep(keep_dir, to, keep_last=1, now=lambda: T0 + timedelta(days=1))
    assert not later.ok
    assert later.exit_code == 1
    assert later.folder is not None
    assert later.folder.name == "2026-10-31_0300_damaged"
    assert later.problem == "damaged: page 7: btree damaged"
    assert good_backups(to / "Movies") == [first.folder]  # not pushed out by a bad copy
    monkeypatch.undo()
    again = backup_keep(keep_dir, to, keep_last=1, now=lambda: T0 + timedelta(days=2))
    assert again.ok
    assert again.deleted == [first.folder]
    assert later.folder.is_dir()  # damaged copies are left for a person to look at
    log = (to / "backup.log").read_text(encoding="utf-8").splitlines()
    assert "  FAILED  Movies.keep: damaged: page 7" in log[1]


def test_check_database_finds_a_file_that_isnt_one(tmp_path: Path) -> None:
    bad = tmp_path / "keep.db"
    bad.write_bytes(b"not a database, just some bytes" * 200)
    assert check_database(bad) != ""


def test_two_runs_in_the_same_minute(keep: tuple[Path, Engine], tmp_path: Path) -> None:
    keep_dir, _ = keep
    names = [backup_keep(keep_dir, tmp_path / "b", now=lambda: T0).folder for _ in range(2)]
    assert [p.name for p in names if p] == ["2026-10-30_0300", "2026-10-30_0300-2"]
    assert len(good_backups(tmp_path / "b" / "Movies")) == 2


def test_copies_in_one_minute_are_kept_in_order(tmp_path: Path) -> None:
    for name in ["2026-10-30_0300", "2026-10-30_0300-10", "2026-10-30_0300-2", "x_damaged"]:
        (tmp_path / name).mkdir()
    assert [p.name for p in good_backups(tmp_path)] == [
        "2026-10-30_0300",
        "2026-10-30_0300-2",
        "2026-10-30_0300-10",
    ]


def test_what_isnt_started(keep: tuple[Path, Engine], tmp_path: Path) -> None:
    keep_dir, _ = keep
    with pytest.raises(BackupError, match="isn't a keep"):
        backup_keep(tmp_path / "nowhere", tmp_path / "b")
    with pytest.raises(BackupError, match="inside the keep"):
        backup_keep(keep_dir, keep_dir / "backups")
    with pytest.raises(BackupError, match="1 or more"):
        backup_keep(keep_dir, tmp_path / "b", keep_last=0)
    assert not (tmp_path / "b").exists()


def test_the_command_line(
    keep: tuple[Path, Engine],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keep_dir, _ = keep
    to = tmp_path / "backups"
    args = ["tagalot", "--backup", str(keep_dir), "--to", str(to), "--keep-last", "3"]
    monkeypatch.setattr(sys, "argv", args)
    assert main() == 0
    out = capsys.readouterr().out.splitlines()
    assert "  ok  Movies.keep -> " in out[0]
    assert len(good_backups(to / "Movies")) == 1
    monkeypatch.setattr(sys, "argv", ["tagalot", "--backup", str(keep_dir)])
    assert main() == 2
    assert capsys.readouterr().out.startswith("error: --to FOLDER is needed")
    report = tmp_path / "report.txt"
    args = ["tagalot", "--backup", str(keep_dir), "--to", str(to), "--keep-last", "x"]
    monkeypatch.setattr(sys, "argv", [*args, "--output", str(report)])
    assert main() == 2
    assert "--keep-last needs a number" in report.read_text(encoding="utf-8")
