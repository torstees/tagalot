"""Entry point for the ``tagalot`` console script and ``python -m tagalot``."""

import faulthandler
import logging
import sys
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)


def main() -> int:
    """Start Tagalot and return the process exit code. ``--check`` reports what this
    build can do and exits; ``--version`` prints the version; ``--new-theme ID`` and
    ``--check-theme FILE`` help theme authors (``theme_tools``); ``--backup KEEP --to FOLDER``
    copies a keep and ``--export-tags KEEP --to FILE`` exports its tags (``backup_tool``).
    With ``--output FILE`` they write to the file instead (a windowed Windows build has no
    console)."""
    if is_console_twin() and not any(c in sys.argv[1:] for c in COMMANDS):
        # tagalot-cli.exe only runs commands; the window is tagalot.exe (#610).
        print(CLI_USAGE)
        return 2
    if "--version" in sys.argv[1:]:
        import tagalot

        _output(sys.argv[1:])(f"Tagalot {tagalot.__version__}")
        return 0
    if "--new-theme" in sys.argv[1:]:
        from tagalot.theme_tools import run_new_theme

        return run_new_theme(sys.argv[1:], _output(sys.argv[1:]))
    if "--check-theme" in sys.argv[1:]:
        from tagalot.theme_tools import run_check_theme

        return run_check_theme(sys.argv[1:], _output(sys.argv[1:]))
    if "--backup" in sys.argv[1:]:
        from tagalot.backup_tool import run_backup

        return run_backup(sys.argv[1:], _output(sys.argv[1:]))
    if "--export-tags" in sys.argv[1:]:
        from tagalot.backup_tool import run_export_tags

        return run_export_tags(sys.argv[1:], _output(sys.argv[1:]))
    from tagalot.core.rar_tools import configure_rar_tools

    configure_rar_tools()  # WinRAR or 7-Zip off PATH, on Windows (#452)
    if "--check" in sys.argv[1:]:
        from tagalot.selfcheck import run_check

        return run_check(_output(sys.argv[1:]))
    logging.basicConfig(level=logging.INFO)
    if sys.stderr is not None:  # a windowed build has no console to write to
        # A crash inside Qt prints where Python was, rather than nothing (#553's review).
        faulthandler.enable()
    logger.info("Tagalot starting")

    # Imported here so that importing this module does not load Qt.
    from tagalot.ui import app

    return app.run(sys.argv)


COMMANDS = (
    "--version",
    "--check",
    "--new-theme",
    "--check-theme",
    "--backup",
    "--export-tags",
)
CLI_USAGE = """usage: tagalot-cli --backup KEEP --to FOLDER [--keep-last N] [--tags]
       tagalot-cli --export-tags KEEP --to FILE.yaml [--items] [--allow-watched]
       tagalot-cli --check | --version
Add --output FILE to any of them to write what it prints to FILE (for scheduled tasks).
tagalot-cli runs Tagalot's commands in a console, for scheduled tasks; tagalot.exe opens
the window. See https://torstees.github.io/tagalot/#/guide/backups"""


def is_console_twin() -> bool:
    """Running as ``tagalot-cli``, the packaged Windows build's console executable."""
    return bool(getattr(sys, "frozen", False)) and Path(sys.executable).stem.lower() == (
        "tagalot-cli"
    )


def _output(args: list[str]) -> Callable[[str], None]:
    """Where ``--check`` and ``--version`` write: ``--output FILE``, else standard output."""
    if "--output" in args and args.index("--output") + 1 < len(args):
        path = Path(args[args.index("--output") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)  # a scheduled run's first time (#610)
        path.write_text("", encoding="utf-8")

        def to_file(line: str) -> None:
            with path.open("a", encoding="utf-8") as f:
                print(line, file=f)

        return to_file
    return print


if __name__ == "__main__":
    sys.exit(main())
