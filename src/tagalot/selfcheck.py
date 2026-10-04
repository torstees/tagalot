"""``tagalot --check``: report what this Tagalot can do, without opening a window (#129).

It prints the version, Python, Qt, SQLite's FTS5, the built-in themes found, the theme
template and the icon (files the build must carry along), and each file reader (Pillow,
mutagen, MediaInfo, 7z, RAR), and exits 1 if something Tagalot needs is missing.
Packaged builds run it in CI, since a frozen app is where a module or a native library
quietly goes missing.
"""

import platform
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import tagalot

BUILTIN = ("assets2d", "books", "generic", "movies", "music")
"""The built-in themes every build must find."""


def run_check(out: Callable[[str], None] = print) -> int:
    """Print the report; return the exit code (0 when everything needed is there)."""
    failures: list[str] = []

    def line(name: str, value: str, ok: bool = True, needed: bool = True) -> None:
        mark = "ok" if ok else ("MISSING" if needed else "unavailable")
        out(f"{name:<14} {mark:<12} {value}")
        if not ok and needed:
            failures.append(name)

    out(f"Tagalot {tagalot.__version__}")
    line("Python", f"{platform.python_version()} ({sys.platform}, {platform.machine()})")
    try:
        import PySide6
        from PySide6.QtCore import qVersion

        line("Qt", f"PySide6 {PySide6.__version__}, Qt {qVersion()}")
    except Exception as e:
        line("Qt", f"{type(e).__name__}: {e}", ok=False)

    from tagalot.core.db import check_sqlite_support

    problem = check_sqlite_support()
    line("SQLite FTS5", problem or "available", ok=problem is None)

    from tagalot.themes.loader import load_themes

    # Only the built-in themes: a user's own themes folder isn't part of the build.
    catalog = load_themes(user_dir=Path(tempfile.gettempdir()) / "tagalot-no-user-themes")
    missing = [t for t in BUILTIN if t not in catalog.themes]
    line(
        "Themes",
        ", ".join(sorted(catalog.themes))
        + (f" (missing: {', '.join(missing)})" if missing else ""),
        ok=not missing,
    )

    try:
        from tagalot.theme_tools import template_source

        line("Template", f"theme template, {len(template_source().splitlines())} lines")
    except Exception as e:
        line("Template", f"{type(e).__name__}: {e}", ok=False)

    from tagalot.resources import icon_files

    found = [size for size, path in icon_files().items() if path.is_file()]
    line("Icon", f"{len(found)} of {len(icon_files())} sizes", ok=len(found) == len(icon_files()))

    for name, probe, needed in READERS:
        try:
            line(name, probe(), needed=needed)
        except Exception as e:
            line(name, f"{type(e).__name__}: {e}", ok=False, needed=needed)

    out("All good." if not failures else f"Missing: {', '.join(failures)}")
    return 1 if failures else 0


def _pillow() -> str:
    import PIL

    return f"Pillow {PIL.__version__}"


def _mutagen() -> str:
    import mutagen

    return f"mutagen {mutagen.version_string}"


def _mediainfo() -> str:
    from pymediainfo import MediaInfo

    if not MediaInfo.can_parse():
        raise RuntimeError("the MediaInfo library isn't loadable")
    return "MediaInfo library loaded"


def _py7zr() -> str:
    import py7zr

    return f"py7zr {py7zr.__version__}"


def _rarfile() -> str:
    import rarfile

    tool = rarfile.tool_setup(sevenzip=True, sevenzip2=True, unrar=True, unar=True, bsdtar=True)
    del tool
    return f"rarfile {rarfile.__version__} (with an unrar tool)"


READERS: list[tuple[str, Callable[[], str], bool]] = [
    ("Pillow", _pillow, True),
    ("mutagen", _mutagen, True),
    ("MediaInfo", _mediainfo, True),
    ("py7zr", _py7zr, True),
    ("RAR", _rarfile, False),  # optional: needs an external unrar (DESIGN.md §3)
]
"""File readers: (name, probe, whether Tagalot needs it)."""
