"""Entry point for the ``tagalot`` console script and ``python -m tagalot``."""

import logging
import sys
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)


def main() -> int:
    """Start Tagalot and return the process exit code. ``--check`` reports what this
    build can do and exits; ``--version`` prints the version; ``--new-theme ID`` and
    ``--check-theme FILE`` help theme authors (``theme_tools``). With ``--output FILE`` they
    write to the file instead (a windowed Windows build has no console)."""
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
    if "--check" in sys.argv[1:]:
        from tagalot.selfcheck import run_check

        return run_check(_output(sys.argv[1:]))
    logging.basicConfig(level=logging.INFO)
    logger.info("Tagalot starting")

    # Imported here so that importing this module does not load Qt.
    from tagalot.ui import app

    return app.run(sys.argv)


def _output(args: list[str]) -> Callable[[str], None]:
    """Where ``--check`` and ``--version`` write: ``--output FILE``, else standard output."""
    if "--output" in args and args.index("--output") + 1 < len(args):
        path = Path(args[args.index("--output") + 1])
        path.write_text("", encoding="utf-8")

        def to_file(line: str) -> None:
            with path.open("a", encoding="utf-8") as f:
                print(line, file=f)

        return to_file
    return print


if __name__ == "__main__":
    sys.exit(main())
