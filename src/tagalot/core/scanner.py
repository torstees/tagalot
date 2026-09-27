"""Walking roots, diffing against the database, and move detection."""

import logging
import os
import re
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass

from tagalot.core.models import ResourceKind

logger = logging.getLogger(__name__)

ErrorHandler = Callable[[str, OSError], None]
"""Called with the relative path of a folder or file that could not be read, and the error."""

DirRule = bool | Callable[[str], bool]
"""Whether to record directories as resources: all, none, or those a predicate accepts."""


@dataclass(frozen=True)
class WalkEntry:
    """A file or directory found under a root, before comparison with the database."""

    relpath: str
    """POSIX-style path relative to the root, e.g. ``Artist/Album/01 Song.flac``."""
    kind: ResourceKind
    ext: str
    """Lowercase extension with the dot (``.flac``), or empty; always empty for directories."""
    size: int | None
    """File size in bytes; ``None`` for directories."""
    mtime_ns: int


def compile_excludes(patterns: Iterable[str]) -> re.Pattern[str] | None:
    """Compile root exclude globs into one case-insensitive regex over POSIX relative paths.

    ``*`` and ``?`` stay within one path segment; ``**`` spans segments. ``**/x`` matches ``x``
    at any depth, including the top, and ``x/**`` matches the folder ``x`` itself and all of
    its contents, so an excluded folder is pruned rather than walked.
    """
    parts = [_glob_to_regex(p) for p in patterns if p.strip()]
    if not parts:
        return None
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


def _glob_to_regex(pattern: str) -> str:
    pattern = pattern.strip().replace("\\", "/").strip("/")
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and (end := pattern.find("]", i + 2)) != -1:
            body = pattern[i + 1 : end]
            negate = body.startswith("!")
            chars = body[negate:].replace("\\", "\\\\").replace("^", "\\^")  # keep ranges
            out.append("[" + ("^" if negate else "") + chars + "]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out) + r"\Z"


def walk_root(
    root_path: str,
    *,
    exclude: Iterable[str] = (),
    extensions: Collection[str] | None = None,
    dirs: DirRule = False,
    on_error: ErrorHandler | None = None,
) -> Iterator[WalkEntry]:
    """Yield the files (and optionally directories) under ``root_path``.

    - ``exclude``: glob patterns (see :func:`compile_excludes`); matching folders are pruned.
    - ``extensions``: lowercase extensions such as ``{".flac", ".jpg"}``; ``None`` keeps all.
    - ``dirs``: whether directories become entries (the root itself never does).
    - Symlinked folders and junctions are not followed; symlinked files are included.
    - Unreadable folders and files go to ``on_error`` and the walk continues.

    Entries are yielded in name order within each folder, so results are deterministic.
    """
    excluded = compile_excludes(exclude)
    wanted = {e.lower() for e in extensions} if extensions is not None else None
    report = on_error or _log_error
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        try:
            with os.scandir(os.path.join(root_path, rel_dir) if rel_dir else root_path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            report(rel_dir, e)
            continue
        subdirs: list[str] = []
        for entry in entries:
            relpath = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if excluded is not None and excluded.match(relpath):
                continue
            try:
                if entry.is_dir(follow_symlinks=False) and not entry.is_junction():
                    if dirs is True or (callable(dirs) and dirs(relpath)):
                        yield WalkEntry(
                            relpath=relpath,
                            kind=ResourceKind.DIR,
                            ext="",
                            size=None,
                            mtime_ns=entry.stat(follow_symlinks=False).st_mtime_ns,
                        )
                    subdirs.append(relpath)
                elif entry.is_file():
                    ext = os.path.splitext(entry.name)[1].lower()
                    if wanted is not None and ext not in wanted:
                        continue
                    st = entry.stat()
                    yield WalkEntry(
                        relpath=relpath,
                        kind=ResourceKind.FILE,
                        ext=ext,
                        size=st.st_size,
                        mtime_ns=st.st_mtime_ns,
                    )
            except OSError as e:
                report(relpath, e)
        stack.extend(reversed(subdirs))  # pop in name order: depth-first, sorted


def _log_error(relpath: str, error: OSError) -> None:
    logger.warning("Cannot read %s: %s", relpath or "(root)", error)
