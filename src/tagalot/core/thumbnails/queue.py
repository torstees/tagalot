"""Making thumbnails in the background after a scan (DESIGN.md §6 step 7, §10).

Grids make thumbnails as they show them. So that a scan's new pictures are ready before
anyone browses, :class:`ThumbnailQueue` resolves the scan's affected entities in the
background, :data:`THREADS` at a time (#275) and roughly newest first, while the grids keep
their own workers:

- **Which:** :func:`entities_needing_thumbnails`: entities with no remembered source
  (``thumb_resource_id``; ingest clears it when links change, and new entities have none),
  and those whose remembered file was ingested again since the scan began (changed in
  place).
- Each one is resolved as a grid would (§10), so it is cached at the keep's size and
  remembered; files that can't be read are remembered as failed for the session.
- Starting again (the next scan) replaces the current run; :meth:`ThumbnailQueue.stop`
  (called when the keep closes) waits for its threads, which finish at most the thumbnails
  they are making.
"""

import logging
import threading
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, Connection, or_, select

from tagalot.core.models import Entity, Resource
from tagalot.core.thumbnails.resolve import ThumbnailResolver, ThumbnailResult

logger = logging.getLogger(__name__)

Progress = Callable[[int, int], None]
"""``progress(done, total)``, called from the queue's thread."""

THREADS = 4
"""Thumbnails made at once in the background: reading a file (a share's latency) and
decoding it mostly release the GIL. The grids have their own threads (``ui.thumbnails``),
so browsing isn't starved."""


def entities_needing_thumbnails(conn: Connection, since: datetime | None = None) -> list[int]:
    """Entity ids to resolve, newest first: no remembered source, or (with ``since``) one
    whose file was ingested at or after ``since``."""
    remembered = Resource.id == Entity.thumb_resource_id
    changed = (
        select(Resource.id).where(remembered, Resource.ingested_at >= since).exists()
        if since is not None
        else None
    )
    condition: ColumnElement[bool] = Entity.thumb_resource_id.is_(None)
    if changed is not None:
        condition = or_(condition, changed)
    return list(conn.scalars(select(Entity.id).where(condition).order_by(Entity.id.desc())))


@dataclass(frozen=True)
class QueueResult:
    done: int
    """Entities resolved."""
    pictures: int
    """Of those, how many have a picture (the rest show an icon)."""
    stopped: bool
    """Stopped before the end (a newer run, or the keep closing)."""


class ThumbnailQueue:
    """Resolves a list of entities in the background: one thread runs the list, handing
    the work to :data:`THREADS` more. See the module docstring."""

    def __init__(self, resolver: ThumbnailResolver) -> None:
        self.resolver = resolver
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._closed = False
        self.remaining = 0

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(
        self,
        entity_ids: Sequence[int],
        progress: Progress | None = None,
        done: Callable[[QueueResult], None] | None = None,
    ) -> None:
        """Resolve these entities in the background, replacing any current run.
        ``progress`` and ``done`` are called from the queue's thread."""
        self.stop()
        ids = list(entity_ids)
        stop = threading.Event()
        with self._lock:
            if self._closed:  # the keep is closing: nothing more starts
                return
            self._stop = stop
            self.remaining = len(ids)
            self._thread = threading.Thread(
                target=self._run,
                args=(ids, stop, progress, done),
                name="thumbnail-queue",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        """Stop for good: the keep is closing, and later :meth:`start` calls do nothing."""
        with self._lock:
            self._closed = True
        self.stop()

    def stop(self) -> None:
        """Stop the current run and wait for it (at most the thumbnails in progress)."""
        with self._lock:
            thread, self._thread = self._thread, None
            self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _run(
        self,
        ids: list[int],
        stop: threading.Event,
        progress: Progress | None,
        done: Callable[[QueueResult], None] | None,
    ) -> None:
        made = pictures = 0
        total = len(ids)

        def finish(entity_id: int, future: Future[ThumbnailResult]) -> bool:
            """Count one finished thumbnail; False once the run is stopping."""
            nonlocal made, pictures
            try:
                result = future.result()
            except Exception:  # a closing keep, or a bug in a theme's provider
                if stop.is_set():
                    return False
                logger.exception("Background thumbnail for entity %d failed", entity_id)
            else:
                pictures += result.thumbnail is not None
            made += 1
            self.remaining = total - made
            if progress is not None:
                _tell(progress, made, total)
            return not stop.is_set()

        # A short window of work keeps the threads busy and the order close to the list's
        # (newest first), and leaves little to cancel when the run stops.
        pool = ThreadPoolExecutor(THREADS, thread_name_prefix="thumbnail-queue")
        window: deque[tuple[int, Future[ThumbnailResult]]] = deque()
        try:
            running = True
            for entity_id in ids:
                if stop.is_set():
                    running = False
                    break
                window.append((entity_id, pool.submit(self.resolver.resolve, entity_id)))
                if len(window) >= THREADS * 2 and not finish(*window.popleft()):
                    running = False
                    break
            while running and window:
                running = finish(*window.popleft())
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        self.remaining = 0
        if done is not None and not stop.is_set():
            _tell(done, QueueResult(made, pictures, stopped=made < total))


def _tell(callback: Callable[..., None], *args: object) -> None:
    """Report to the UI; a window already gone doesn't stop the queue."""
    try:
        callback(*args)
    except Exception:
        logger.debug("Thumbnail queue: report not delivered", exc_info=True)
