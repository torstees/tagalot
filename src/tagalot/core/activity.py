"""What went wrong while the keep was open, for the activity panel (DESIGN.md §12).

A :class:`ProblemLog` belongs to an open keep (``KeepSession.problems``). Scans add what
their reports hold (:func:`problems_from_report`): folders and files that couldn't be read,
ingest failures and the theme's warnings, and roots that were offline. The thumbnail
resolver adds files it couldn't make a picture of. The log is kept for the session only
(the newest :data:`MAX_PROBLEMS`), is safe to use from any thread, and counts its changes
so the UI can notice them cheaply.
"""

import threading
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tagalot.core.scanjob import ScanReport

MAX_PROBLEMS = 1000

READ, INGEST, WARNING, THUMBNAIL, OFFLINE = "read", "ingest", "warning", "thumbnail", "offline"
SCAN = "scan"
KIND_LABELS = {
    READ: "Can't read",
    INGEST: "Not added",
    WARNING: "Warning",
    THUMBNAIL: "No thumbnail",
    OFFLINE: "Offline",
    SCAN: "Scan failed",
}
"""How each kind of problem is named in the panel."""


@dataclass(frozen=True)
class Problem:
    kind: str
    message: str
    root_id: str | None = None
    relpath: str | None = None
    """Relative to the root (scans), or ``None``."""
    path: str | None = None
    """This computer's path, when known (thumbnails)."""
    when: datetime = field(default_factory=lambda: datetime.now(UTC))


def problems_from_report(report: ScanReport) -> list[Problem]:
    """What a scan of one root reported, as problems."""
    root = report.root_id
    if not report.online:
        return [Problem(OFFLINE, report.error or "unreachable", root)]
    found = [
        Problem(READ, message, root, relpath or None) for relpath, message in report.read_errors
    ]
    found += [Problem(INGEST, message, root, relpath) for relpath, message in report.ingest_errors]
    found += [
        Problem(WARNING, w.message, w.root_id or root, w.relpath) for w in report.ingest_warnings
    ]
    return found


def thumbnail_problems(problems: Iterable[tuple[str, str]]) -> list[Problem]:
    """The resolver's ``(path, message)`` pairs as problems."""
    return [
        Problem(THUMBNAIL, message, path=None if where.startswith("entity ") else where)
        for where, message in problems
    ]


class ProblemLog:
    """The session's problems, newest last; see the module docstring."""

    def __init__(self, limit: int = MAX_PROBLEMS) -> None:
        self._items: deque[Problem] = deque(maxlen=limit)
        self._lock = threading.Lock()
        self.version = 0
        """Changes whenever problems are added or cleared."""

    def add(self, problems: Sequence[Problem]) -> None:
        if not problems:
            return
        with self._lock:
            self._items.extend(problems)
            self.version += 1

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.version += 1

    def items(self) -> list[Problem]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
