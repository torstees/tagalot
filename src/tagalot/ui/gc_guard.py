"""Garbage collection on the GUI thread only (#406).

Python's cycle collector runs on whichever thread happens to allocate when a generation
fills up, often a worker. A cycle it frees there can hold Qt objects that belong to the GUI
thread (a widget captured by a callback, a relay), and deleting those from a worker crashes
the process (seen as a segfault "Garbage-collecting" in a worker on macOS). So automatic
collection is turned off, and a timer on the GUI thread collects instead, by the same
thresholds Python would use.
"""

import gc

from PySide6.QtCore import QObject, QTimer

INTERVAL_MS = 500
"""How often the GUI thread checks whether a generation needs collecting."""


class GuiThreadCollector(QObject):
    """Collects garbage on the thread it lives on (the GUI thread), never in workers."""

    def __init__(self, parent: QObject | None = None, interval_ms: int = INTERVAL_MS) -> None:
        super().__init__(parent)
        self.thresholds = gc.get_threshold()
        gc.disable()
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.check)
        self._timer.start()

    def check(self) -> int:
        """Collect the oldest generation over its threshold, if any; the number freed."""
        counts = gc.get_count()
        generation = max(
            (g for g in range(3) if counts[g] > self.thresholds[g]),
            default=None,
        )
        return 0 if generation is None else gc.collect(generation)


def collect_on_gui_thread(app: QObject) -> GuiThreadCollector:
    """Turn off automatic collection and collect on ``app``'s thread instead."""
    existing = app.findChild(GuiThreadCollector)
    return existing if existing is not None else GuiThreadCollector(app)
