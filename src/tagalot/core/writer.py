"""The single DB writer (AGENTS.md rule 5, DESIGN.md §6 "Threading model").

One thread owns the keep's write engine and applies jobs in submission order, each in its own
short transaction. Workers never open write transactions; they submit a function that
receives a :class:`~sqlalchemy.Connection` and get a :class:`~concurrent.futures.Future` for
its result. Nothing here imports Qt: the UI turns futures into signals.
"""

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, TypeVar

from sqlalchemy import Connection, Engine

logger = logging.getLogger(__name__)

T = TypeVar("T")

Job = tuple[Callable[[Connection], Any], "Future[Any]"]


class WriterClosedError(RuntimeError):
    """Work was submitted after the writer was closed."""


class DbWriter:
    """Applies write jobs one at a time on a dedicated thread."""

    def __init__(self, engine: Engine, *, name: str = "db-writer") -> None:
        self._engine = engine
        self._queue: queue.SimpleQueue[Job | None] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, work: Callable[[Connection], T]) -> "Future[T]":
        """Queue ``work`` to run in its own transaction; returns a future for its result.

        If ``work`` raises, its transaction is rolled back, the exception is set on the
        future, and the writer carries on with the next job.
        """
        future: Future[T] = Future()
        with self._lock:
            if self._closed:
                raise WriterClosedError("the DB writer is closed")
            self._queue.put((work, future))
        return future

    def run(self, work: Callable[[Connection], T], timeout: float | None = None) -> T:
        """Submit ``work`` and wait for its result. Not callable from inside a job."""
        if threading.current_thread() is self._thread:
            raise RuntimeError("a DB writer job cannot wait for another job (deadlock)")
        return self.submit(work).result(timeout)

    def close(self, timeout: float | None = None) -> None:
        """Finish queued jobs, stop the thread, and dispose of the engine."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)
        self._thread.join(timeout)
        self._engine.dispose()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> "DbWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _run(self) -> None:
        while (job := self._queue.get()) is not None:
            work, future = job
            if not future.set_running_or_notify_cancel():
                continue  # cancelled while queued
            try:
                with self._engine.begin() as conn:
                    result = work(conn)
            except BaseException as e:
                logger.exception("DB writer job failed")
                future.set_exception(e)
            else:
                future.set_result(result)
