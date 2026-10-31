"""``tagalot --backup KEEP --to FOLDER [--keep-last N]`` (#608): a checked copy of a keep,
for a scheduler (cron, Windows Task Scheduler) to run. The work is ``core/backup.py``."""

from collections.abc import Callable, Sequence
from pathlib import Path

from tagalot.core.backup import USAGE, BackupError, backup_keep, log_line

Out = Callable[[str], None]

USAGE_TEXT = "usage: tagalot --backup KEEP --to FOLDER [--keep-last N]"


def run_backup(args: Sequence[str], out: Out = print) -> int:
    """Run ``--backup`` from the command line's arguments; returns the exit code."""
    try:
        keep, to, keep_last = parse_backup_args(args)
        result = backup_keep(keep, to, keep_last)
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
