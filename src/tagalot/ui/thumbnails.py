"""Loading thumbnails for the grid in the background (DESIGN.md §10, §12).

:class:`ThumbnailLoader` resolves thumbnails on its own small thread pool, so a screenful of
thumbnails over a network share never delays searches. The newest requests go first: when
the user scrolls quickly, the cards now on screen load before the ones scrolled past.
Decoded images are kept in a bounded LRU; :attr:`ThumbnailLoader.ready` says which entity's
thumbnail arrived.
"""

import logging
from collections import OrderedDict
from dataclasses import dataclass

import shiboken6
from PySide6.QtCore import QObject, QThreadPool, Signal
from PySide6.QtGui import QIcon, QImage
from PySide6.QtWidgets import QApplication, QStyle

from tagalot.core.thumbnails.resolve import ThumbnailResolver
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

THREADS = 4
"""Thumbnails resolved at once: enough to hide network latency, few enough to stay polite."""

MAX_IMAGES = 600
"""Decoded thumbnails kept in memory (a few screenfuls at large sizes)."""

MAX_WANTED = 400
"""Requests waiting at most; the oldest (long scrolled past) are dropped."""

PRESETS = (("Small", 64), ("Medium", 128), ("Large", 256))
"""The View → Thumbnail size choices below the maximum."""

ZOOM_STEPS = (48, 64, 80, 96, 128, 160, 192, 256, 320, 384, 512, 640, 768, 1024, 1536, 2048)
"""Sizes Ctrl+wheel and Zoom in / out step through (never above the keep's maximum)."""


def size_presets(maximum: int) -> list[tuple[str, int]]:
    """The menu's sizes for a keep whose thumbnails are made at ``maximum``: the presets
    below it, then "Largest" (the maximum itself)."""
    return [(name, size) for name, size in PRESETS if size < maximum] + [("Largest", maximum)]


def zoom_sizes(maximum: int) -> list[int]:
    return [s for s in ZOOM_STEPS if s < maximum] + [maximum]


def zoomed(size: int, step: int, maximum: int) -> int:
    """The next size from ``size``, one step bigger (``step`` > 0) or smaller."""
    sizes = zoom_sizes(maximum)
    if step > 0:
        return next((s for s in sizes if s > size), sizes[-1])
    return next((s for s in reversed(sizes) if s < size), sizes[0])


def clamp_size(size: object, default: int, maximum: int) -> int:
    """A remembered size, kept within the zoom range (the maximum may have shrunk)."""
    if not isinstance(size, int) or isinstance(size, bool):
        size = default
    return max(ZOOM_STEPS[0], min(size, maximum))


@dataclass(frozen=True)
class LoadedThumbnail:
    """An entity's picture, or else the name of the icon to show."""

    image: QImage | None
    icon: str | None


class ThumbnailLoader(QObject):
    """Loads entity thumbnails in the background for every page of one window."""

    ready = Signal(int)
    """An entity's thumbnail (or icon) is now available from :meth:`get`."""

    def __init__(self, resolver: ThumbnailResolver, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.resolver = resolver
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(THREADS)
        self._loaded: OrderedDict[int, LoadedThumbnail] = OrderedDict()
        self._wanted: OrderedDict[int, None] = OrderedDict()
        self._running: set[int] = set()
        self._generation = 0

    def get(self, entity_id: int) -> LoadedThumbnail | None:
        """The entity's thumbnail if it is loaded; otherwise ``None``, and it is requested."""
        found = self._loaded.get(entity_id)
        if found is not None:
            self._loaded.move_to_end(entity_id)
            return found
        self.request(entity_id)
        return None

    def request(self, entity_id: int) -> None:
        """Load the entity's thumbnail soon (before earlier requests still waiting)."""
        if entity_id in self._loaded or entity_id in self._running:
            return
        self._wanted[entity_id] = None
        self._wanted.move_to_end(entity_id)
        while len(self._wanted) > MAX_WANTED:
            self._wanted.popitem(last=False)
        self._pump()

    def clear(self) -> None:
        """Forget every loaded thumbnail (after a scan, files may have changed)."""
        self._generation += 1
        self._loaded.clear()
        self._wanted.clear()
        self._running.clear()

    @property
    def idle(self) -> bool:
        """Nothing is loading or waiting to load."""
        return not self._wanted and not self._running

    def wait(self, msecs: int = 30_000) -> bool:
        """Wait for running thumbnail jobs (for closing, and tests)."""
        return self.pool.waitForDone(msecs)

    def _pump(self) -> None:
        while self._wanted and len(self._running) < THREADS:
            entity_id, _ = self._wanted.popitem(last=True)  # newest first
            self._running.add(entity_id)
            self._start(entity_id)

    def _start(self, entity_id: int) -> None:
        generation, resolver = self._generation, self.resolver

        def job() -> LoadedThumbnail:
            result = resolver.resolve(entity_id)
            for path, message in result.problems:
                logger.warning("Thumbnail of %s: %s", path, message)
            if result.thumbnail is None:
                return LoadedThumbnail(None, result.icon)
            image = QImage.fromData(result.thumbnail.data)
            if image.isNull():  # a format this Qt can't decode
                return LoadedThumbnail(None, result.icon or "file")
            return LoadedThumbnail(image, None)

        def done(loaded: LoadedThumbnail) -> None:
            self._finished(generation, entity_id, loaded)

        def failed(error: BaseException) -> None:
            logger.warning("Thumbnail of entity %d failed: %s", entity_id, error)
            self._finished(generation, entity_id, LoadedThumbnail(None, "entity"))

        run_in_pool(job, on_done=done, on_error=failed, pool=self.pool)

    def _finished(self, generation: int, entity_id: int, loaded: LoadedThumbnail) -> None:
        if not shiboken6.isValid(self):
            return  # the window closed while this thumbnail was loading
        if generation != self._generation:
            return  # cleared meanwhile: it may be out of date
        self._running.discard(entity_id)
        self._loaded[entity_id] = loaded
        while len(self._loaded) > MAX_IMAGES:
            self._loaded.popitem(last=False)
        self.ready.emit(entity_id)
        self._pump()


_ICONS: dict[str, QStyle.StandardPixmap] = {
    "dir": QStyle.StandardPixmap.SP_DirIcon,
    "folder": QStyle.StandardPixmap.SP_DirIcon,
    "audio": QStyle.StandardPixmap.SP_MediaVolume,
    "video": QStyle.StandardPixmap.SP_MediaPlay,
    "archive": QStyle.StandardPixmap.SP_DriveHDIcon,
    "font": QStyle.StandardPixmap.SP_FileDialogDetailedView,
    "entity": QStyle.StandardPixmap.SP_FileIcon,
}


def icon_for(name: str | None) -> QIcon:
    """The icon for a thumbnail chain's :class:`~tagalot.themes.api.Icon` name; unknown
    names get the generic file icon."""
    pixmap = _ICONS.get(name or "", QStyle.StandardPixmap.SP_FileIcon)
    style = QApplication.style()
    return style.standardIcon(pixmap) if style is not None else QIcon()
