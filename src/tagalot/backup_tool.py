"""Backups from the command line, for a scheduler (cron, Windows Task Scheduler) to run:
``tagalot --backup KEEP --to FOLDER [--keep-last N] [--tags]`` (#608; ``--tags``, #611), a
checked copy of a keep (``core/backup.py``), and ``tagalot --export-tags KEEP --to FILE [--items]
[--allow-watched]`` (#609), the tag export, and ``tagalot --export-keep KEEP --to FILE
[--allow-watched]`` (#612), the keep export (``core/exports.py``)."""

from collections.abc import Callable, Sequence
from pathlib import Path

from tagalot.core.backup import FAILED, OK, USAGE, BackupError, backup_keep, log_line
from tagalot.core.exports import ExportError, export_keep_file, export_tag_file
from tagalot.core.settings import Settings, load_settings

Out = Callable[[str], None]

USAGE_TEXT = "usage: tagalot --backup KEEP --to FOLDER [--keep-last N] [--tags]"
EXPORT_USAGE = "usage: tagalot --export-tags KEEP --to FILE.yaml [--items] [--allow-watched]"
KEEP_USAGE = "usage: tagalot --export-keep KEEP --to FILE.yaml [--allow-watched]"


def run_backup(args: Sequence[str], out: Out = print, settings: Settings | None = None) -> int:
    """Run ``--backup`` from the command line's arguments; returns the exit code. With
    ``--tags``, each copy holds the tag export too."""
    try:
        keep, to, keep_last = parse_backup_args(args)
        result = backup_keep(
            keep, to, keep_last, tags="--tags" in args, settings=settings or load_settings()
        )
    except BackupError as e:
        out(f"error: {e}")
        out(USAGE_TEXT)
        return USAGE
    out(log_line(result))
    if result.folder is not None:
        out(f"   {result.folder}")
    for old in result.deleted:
        out(f"   deleted {old}")
    return result.exit_code


def run_export_tags(args: Sequence[str], out: Out = print, settings: Settings | None = None) -> int:
    """Run ``--export-tags`` from the command line's arguments; returns the exit code:
    0 written, 1 writing failed, 2 the arguments are wrong or the export can't be made."""
    keep, to = _value(args, "--export-tags"), _value(args, "--to")
    problem = (
        "--export-tags needs the keep's folder"
        if keep is None
        else "--to FILE is needed: where the tags go"
        if to is None
        else None
    )
    if problem is not None or keep is None or to is None:
        out(f"error: {problem}")
        out(EXPORT_USAGE)
        return USAGE
    try:
        done = export_tag_file(
            Path(keep),
            Path(to),
            settings or load_settings(),
            items="--items" in args,
            allow_watched="--allow-watched" in args,
        )
    except ExportError as e:
        out(f"error: {e}")
        return USAGE
    except OSError as e:
        out(f"error: couldn't write {to}: {e}")
        return FAILED
    what = f"{done.tags:,} tags" + (f" and {done.items:,} items" if done.items is not None else "")
    out(f"ok  exported {what} to {done.path}")
    if done.pictures:
        out(f"   with {done.pictures:,} pictures in {done.path.stem} pictures")
    if done.watched:
        out(f"   it is in {done.watched}, a watched folder: scans will read it unless skipped")
    return OK


def run_export_keep(args: Sequence[str], out: Out = print, settings: Settings | None = None) -> int:
    """Run ``--export-keep`` from the command line's arguments; returns the exit code, as
    ``--export-tags``'s: 0 written, 1 writing failed, 2 wrong arguments or no export."""
    keep, to = _value(args, "--export-keep"), _value(args, "--to")
    if keep is None or to is None:
        missing = "the keep's folder" if keep is None else "--to FILE: where the export goes"
        out(f"error: --export-keep needs {missing}")
        out(KEEP_USAGE)
        return USAGE
    try:
        done = export_keep_file(
            Path(keep),
            Path(to),
            settings or load_settings(),
            allow_watched="--allow-watched" in args,
        )
    except ExportError as e:
        out(f"error: {e}")
        return USAGE
    except OSError as e:
        out(f"error: couldn't write {to}: {e}")
        return FAILED
    out(f"ok  exported {done.tags:,} tags and {done.items:,} items to {done.path}")
    if done.pictures:
        out(f"   with {done.pictures:,} pictures in {done.path.stem} pictures")
    if done.watched:
        out(f"   it is in {done.watched}, a watched folder: scans will read it unless skipped")
    return OK


def parse_backup_args(args: Sequence[str]) -> tuple[Path, Path, int | None]:
    """The keep, the backup folder, and ``--keep-last`` (None when not given)."""
    keep = _value(args, "--backup")
    to = _value(args, "--to")
    if keep is None:
        raise BackupError("--backup needs the keep's folder")
    if to is None:
        raise BackupError("--to FOLDER is needed: where the backups go")
    last = _value(args, "--keep-last")
    if last is None:
        return Path(keep), Path(to), None
    try:
        return Path(keep), Path(to), int(last)
    except ValueError:
        raise BackupError(f"--keep-last needs a number, not {last!r}") from None


def _value(args: Sequence[str], flag: str) -> str | None:
    """The argument after ``flag``, when there is one that isn't another flag."""
    if flag not in args:
        return None
    i = list(args).index(flag) + 1
    if i >= len(args) or args[i].startswith("--"):
        return None
    return args[i]
