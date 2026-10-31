"""``tagalot --backup``: a complete, checked copy of a keep for a scheduler to make (#608).

A backup is ``keep.db`` (copied with SQLite's backup API, so it is consistent while the
app has the keep open) and ``keep.toml``, in a dated folder ``<to>/<keep>/<date>_<time>/``.
The copy is made under a ``.partial`` name and checked (``PRAGMA integrity_check``): a good
one is renamed to its dated name, a damaged one to ``<name>_damaged``. Only good copies
count toward ``keep_last``, so a damaged copy never pushes out an older good one. Each run
adds one line to ``<to>/backup.log``. Restoring is copying the two files back.

With ``tags`` (#611), the copy also holds the tag export, tags and the items that have them
(``<keep> tags.yaml`` and its pictures), read from the copy itself, so the two agree. The
backup folder can't be in a folder the keep watches (AGENTS.md rule 1)."""

import logging
import os
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from tagalot.core.exports import ExportError, export_tag_file
from tagalot.core.keep import (
    KEEP_DB,
    KEEP_TOML,
    KeepConfig,
    KeepConfigError,
    load_keep_config,
)
from tagalot.core.roots import place_in_roots
from tagalot.core.settings import Settings

logger = logging.getLogger(__name__)

LOG_NAME = "backup.log"
STAMP = "%Y-%m-%d_%H%M"
GOOD = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{4})(?:-(\d+))?")
PARTIAL = ".partial"
DAMAGED = "_damaged"

# Exit codes, documented in the user guide (Backups).
OK = 0
FAILED = 1  # the copy couldn't be made, or it is damaged
USAGE = 2  # the arguments are wrong, or the folder isn't a keep


class BackupError(Exception):
    """A backup can't be started: the message says why (exit code :data:`USAGE`)."""


@dataclass(frozen=True)
class BackupResult:
    """What one run did."""

    keep: str  # the keep's folder name
    started: datetime
    folder: Path | None  # the new copy (renamed ``_damaged`` when the check failed)
    ok: bool
    problem: str = ""  # why it failed, when it did
    size: int = 0  # bytes copied
    deleted: list[Path] = field(default_factory=list)  # old copies removed for keep_last
    tags_problem: str = ""  # why the tag export (``tags``) failed; the copy still counts

    @property
    def exit_code(self) -> int:
        return OK if self.ok and not self.tags_problem else FAILED


def backup_keep(
    keep_dir: Path,
    to: Path,
    keep_last: int | None = None,
    now: Callable[[], datetime] = datetime.now,
    *,
    tags: bool = False,
    settings: Settings | None = None,
) -> BackupResult:
    """Back up the keep in ``keep_dir`` under ``to`` (with its tag export, when ``tags``),
    then delete the oldest good copies beyond ``keep_last``. ``settings`` gives this
    computer's paths of the keep's folders and the theme folders. Raises
    :class:`BackupError` before copying anything when the folder isn't a keep, ``to`` is
    inside it or a folder it watches, or ``keep_last`` is below 1; a failed copy is
    returned, not raised."""
    if keep_last is not None and keep_last < 1:
        raise BackupError("--keep-last must be 1 or more")
    settings = settings or Settings()
    keep_dir, to = keep_dir.resolve(), to.resolve()  # relative paths work too
    db, toml = keep_dir / KEEP_DB, keep_dir / KEEP_TOML
    if not toml.is_file() or not db.is_file():
        raise BackupError(f"{keep_dir} isn't a keep: it needs {KEEP_TOML} and {KEEP_DB}")
    try:
        config = load_keep_config(toml)
    except KeepConfigError as e:
        raise BackupError(f"{toml} can't be read: {e}") from e
    if problem := folder_problem(to, keep_dir, watched_folders(config, settings)):
        raise BackupError(problem)
    started = now()
    home = to / keep_name(keep_dir)
    target = _free_name(home, started.strftime(STAMP))
    partial = target.with_name(target.name + PARTIAL)
    try:
        partial.mkdir(parents=True)
        _copy_database(db, partial / KEEP_DB)
        shutil.copy2(toml, partial / KEEP_TOML)
        problem = check_database(partial / KEEP_DB)
        tags_problem = _export_tags(partial, keep_dir, settings) if tags and not problem else ""
    except Exception as e:  # any failure: clean up, log it, exit 1 (never a traceback)
        logger.exception("Backing up %s failed", keep_dir)
        shutil.rmtree(partial, ignore_errors=True)
        result = BackupResult(keep_dir.name, started, None, False, f"couldn't copy: {e}")
        _log(to, result)
        return result
    size = sum(p.stat().st_size for p in partial.rglob("*") if p.is_file())
    if problem:
        damaged = target.with_name(target.name + DAMAGED)
        partial.rename(damaged)
        result = BackupResult(keep_dir.name, started, damaged, False, f"damaged: {problem}", size)
    else:
        partial.rename(target)
        deleted = prune(home, keep_last) if keep_last is not None else []
        result = BackupResult(keep_dir.name, started, target, True, "", size, deleted, tags_problem)
    _log(to, result)
    return result


def watched_folders(config: KeepConfig, settings: Settings) -> dict[str, tuple[str, str | None]]:
    """The keep's folders on this computer: root id -> (its name, its path)."""
    return {r.id: (r.name, settings.root_path(config.id, r)) for r in config.roots}


def folder_problem(folder: Path, keep_dir: Path, watched: dict[str, tuple[str, str | None]]) -> str:
    """Why ``folder`` can't hold the keep's backups, or ``""``: it may not be inside the
    keep's folder, nor in a folder the keep watches (Tagalot never writes there)."""
    folder, keep_dir = folder.resolve(), keep_dir.resolve()
    if folder.is_relative_to(keep_dir):
        return "The backup folder can't be inside the keep's folder."
    paths = {k: p for k, (_, p) in watched.items()}
    place = place_in_roots(str(folder), paths)
    same = next((k for k, p in paths.items() if p and _same_folder(folder, p)), None)
    root_id = place[0] if place is not None else same
    if root_id is not None:
        return (
            f"The backup folder can't be in {watched[root_id][0]}, a folder this keep "
            "watches: Tagalot never writes there."
        )
    return ""


def _same_folder(folder: Path, path: str) -> bool:
    return os.path.normcase(os.path.abspath(folder)) == os.path.normcase(os.path.abspath(path))


def _export_tags(copy: Path, keep_dir: Path, settings: Settings) -> str:
    """Write the tag export, with items, into the copy, read from the copy; ``""`` when it
    was written, else why not."""
    target = copy / f"{keep_name(keep_dir)} tags.yaml"
    try:
        export_tag_file(copy, target, settings, items=True)
    except ExportError as e:
        return str(e)
    except Exception as e:  # never a traceback from a scheduled run
        logger.exception("Exporting the tags of %s failed", keep_dir)
        return f"{type(e).__name__}: {e}"
    return ""


def keep_name(keep_dir: Path) -> str:
    """The folder a keep's backups go in: its folder's name without ``.keep``."""
    name = keep_dir.name
    return name[: -len(".keep")] if name.lower().endswith(".keep") else name


def good_backups(home: Path) -> list[Path]:
    """A keep's good copies, oldest first (their names sort by date)."""
    if not home.is_dir():
        return []
    return sorted((p for p in home.iterdir() if p.is_dir() and GOOD.fullmatch(p.name)), key=_order)


def _order(folder: Path) -> tuple[str, int]:
    """A copy's date, then its number among copies made in the same minute (``-2``...)."""
    match = GOOD.fullmatch(folder.name)
    assert match is not None
    return match[1], int(match[2] or 1)


def prune(home: Path, keep_last: int) -> list[Path]:
    """Delete the oldest good copies beyond ``keep_last``; damaged and unfinished copies
    are left alone. Returns the deleted folders."""
    deleted = []
    for old in good_backups(home)[:-keep_last]:
        try:
            shutil.rmtree(old)
        except OSError:
            logger.exception("Couldn't delete the old backup %s", old)
        else:
            deleted.append(old)
    return deleted


def check_database(path: Path) -> str:
    """``""`` when SQLite finds the database sound, else what it found."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    except sqlite3.DatabaseError as e:  # not a database at all
        return str(e)
    finally:
        conn.close()
    return "" if rows == ["ok"] else "; ".join(rows[:5])


def _copy_database(source: Path, target: Path) -> None:
    """Copy with SQLite's backup API, which sees one consistent state even while the app
    writes (with WAL)."""
    src = sqlite3.connect(source)
    try:
        dest = sqlite3.connect(target)
        try:
            src.backup(dest)
            # One file, without -wal and -shm beside it; the app turns WAL on again on open.
            dest.execute("PRAGMA journal_mode=DELETE")
        finally:
            dest.close()
    finally:
        src.close()


def _free_name(home: Path, stamp: str) -> Path:
    """``home/stamp``, or ``stamp-2`` and on when a run in the same minute has it."""
    n = 1
    while True:
        name = stamp if n == 1 else f"{stamp}-{n}"
        if not any((home / f"{name}{end}").exists() for end in ("", PARTIAL, DAMAGED)):
            return home / name
        n += 1


def log_line(result: BackupResult) -> str:
    """The run's line in ``backup.log``."""
    when = result.started.strftime("%Y-%m-%d %H:%M:%S")
    if not result.ok:
        return f"{when}  FAILED  {result.keep}: {result.problem}"
    assert result.folder is not None
    line = f"{when}  ok  {result.keep} -> {result.folder.name} ({_size(result.size)})"
    if result.deleted:
        line += f", deleted {len(result.deleted)} older"
    if result.tags_problem:
        line += f"; tags FAILED: {result.tags_problem}"
    return line


def _log(to: Path, result: BackupResult) -> None:
    try:
        to.mkdir(parents=True, exist_ok=True)
        with (to / LOG_NAME).open("a", encoding="utf-8") as f:
            print(log_line(result), file=f)
    except OSError:
        logger.exception("Couldn't write %s in %s", LOG_NAME, to)


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    value = n / 1024
    for unit in ("KB", "MB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
