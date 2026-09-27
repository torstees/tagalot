"""QThreadPool jobs and their signals (AGENTS.md rule 4: the GUI thread never blocks on I/O).

- :func:`run_in_pool` runs a function on a worker thread and calls ``on_done`` or
  ``on_error`` **on the GUI thread**.
- :func:`watch_future` does the same for a :class:`~concurrent.futures.Future`, such as one
  returned by the DB writer.
- :class:`ScanController` runs keep scans in the background and reports progress.

Results travel through a relay object that lives on the GUI thread. (A signal connected to a
plain Python callable would run it in the emitting thread, i.e. the worker.)
"""

import logging
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from tagalot.core.scanjob import ScanReport
from tagalot.core.session import KeepSession

logger = logging.getLogger(__name__)

_pending: set["_Relay"] = set()
"""Relays waiting for a result; kept referenced so they aren't garbage-collected early."""


class _Relay(QObject):
    """Receives a worker's result on the GUI thread and calls the right callback once."""

    done = Signal(object)
    error = Signal(object)

    def __init__(
        self,
        on_done: Callable[[Any], None] | None,
        on_error: Callable[[BaseException], None] | None,
    ) -> None:
        super().__init__()
        self._on_done = on_done
        self._on_error = on_error
        self.done.connect(self._deliver_done)
        self.error.connect(self._deliver_error)
        _pending.add(self)

    @Slot(object)
    def _deliver_done(self, result: object) -> None:
        _pending.discard(self)
        if self._on_done is not None:
            self._on_done(result)

    @Slot(object)
    def _deliver_error(self, error: object) -> None:
        _pending.discard(self)
        assert isinstance(error, BaseException)
        if self._on_error is not None:
            self._on_error(error)
        else:
            logger.error("Background job failed", exc_info=error)


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], Any], relay: _Relay) -> None:
        super().__init__()
        self._fn = fn
        self._relay = relay

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as e:  # reported on the GUI thread, never swallowed
            self._relay.error.emit(e)
        else:
            self._relay.done.emit(result)


def run_in_pool[T](
    fn: Callable[[], T],
    *,
    on_done: Callable[[T], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    pool: QThreadPool | None = None,
) -> None:
    """Run ``fn()`` on a worker thread; call ``on_done(result)`` or ``on_error(exc)`` on the
    GUI thread. Call this from the GUI thread."""
    relay = _Relay(on_done, on_error)
    (pool or QThreadPool.globalInstance()).start(_Job(fn, relay))


def watch_future[T](
    future: "Future[T]",
    *,
    on_done: Callable[[T], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    """Call ``on_done`` or ``on_error`` on the GUI thread when ``future`` completes (for
    example a DB-writer job). Call this from the GUI thread."""
    relay = _Relay(on_done, on_error)

    def finished(f: "Future[T]") -> None:
        error = f.exception()
        if error is not None:
            relay.error.emit(error)
        else:
            relay.done.emit(f.result())

    future.add_done_callback(finished)


class ScanController(QObject):
    """Runs "Scan now" for an open keep in the background, one scan at a time."""

    started = Signal()
    progress = Signal(str)
    """A short status message for the status bar."""
    finished = Signal(list)
    """The ``ScanReport`` of each root."""
    failed = Signal(object)
    _progress_from_worker = Signal(str)

    def __init__(self, pool: QThreadPool | None = None) -> None:
        super().__init__()
        self._pool = pool
        self._running = False
        # Re-emit on the GUI thread, so anything connected to `progress` runs there.
        self._progress_from_worker.connect(self._relay_progress)

    @Slot(str)
    def _relay_progress(self, message: str) -> None:
        self.progress.emit(message)

    @property
    def running(self) -> bool:
        return self._running

    def scan(self, session: KeepSession) -> bool:
        """Start scanning every root; returns False if a scan is already running."""
        if self._running:
            return False
        self._running = True
        self.started.emit()
        run_in_pool(
            lambda: session.scan_all(progress=self._progress_from_worker.emit),
            on_done=self._done,
            on_error=self._failed,
            pool=self._pool,
        )
        return True

    def _done(self, reports: list[ScanReport]) -> None:
        self._running = False
        self.finished.emit(reports)

    def _failed(self, error: BaseException) -> None:
        self._running = False
        logger.error("Scan failed", exc_info=error)
        self.failed.emit(error)
