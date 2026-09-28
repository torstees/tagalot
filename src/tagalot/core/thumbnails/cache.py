"""The thumbnail cache: ``thumbs.db`` in the keep folder (DESIGN.md §4, §10 "Cache").

It is disposable: deleting it only costs regenerating thumbnails, so anything unexpected
(a damaged file, another format version) is handled by starting it afresh. Keys hash
everything a thumbnail depends on (:func:`thumb_key`), so a changed file or an upgraded
provider simply misses the cache; stale entries are never read, only cleared with the rest.

It is separate from ``keep.db`` and written directly by the workers that make thumbnails
(one short transaction per thumbnail, serialized by a lock), not through the keep's DB
writer: nothing in it is shared with other data.
"""

import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    event,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import URL
from sqlalchemy.exc import DatabaseError
from sqlalchemy.pool import StaticPool

from tagalot.core.models import utcnow
from tagalot.core.thumbnails.image import Thumbnail

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
"""``PRAGMA user_version`` of ``thumbs.db``; another value means start the file afresh."""

metadata = MetaData()
thumb = Table(
    "thumb",
    metadata,
    Column("key", String, primary_key=True),
    Column("size", Integer, nullable=False),  # the thumbnail size asked for (pixels)
    Column("format", String, nullable=False),  # "webp" or "png"
    Column("width", Integer, nullable=False),
    Column("height", Integer, nullable=False),
    Column("data", LargeBinary, nullable=False),
    Column("created_at", DateTime, nullable=False),
)


def thumb_key(
    resource_id: int,
    file_size: int | None,
    mtime_ns: int | None,
    provider_id: str,
    provider_version: int,
    thumb_size: int,
) -> str:
    """The cache key of a thumbnail: a hash of everything it depends on, so a changed file
    (size or modification time), another provider or provider version, or another thumbnail
    size gives another key."""
    parts = (resource_id, file_size, mtime_ns, provider_id, provider_version, thumb_size)
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


class _OtherVersion(Exception):
    """``thumbs.db`` was written in another format: start it afresh."""


@dataclass(frozen=True)
class CacheStats:
    count: int
    bytes: int


class ThumbCache:
    """``thumbs.db``: get and put encoded thumbnails by key. Safe to use from several
    threads; call :meth:`close` when the keep closes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._engine: Engine | None = None

    # --- reading and writing ---

    def get(self, key: str) -> Thumbnail | None:
        """The thumbnail stored under ``key``, or ``None``."""
        with self._lock:
            engine = self._open()
            with engine.connect() as conn:
                row = conn.execute(
                    select(thumb.c.data, thumb.c.format, thumb.c.width, thumb.c.height).where(
                        thumb.c.key == key
                    )
                ).first()
        return None if row is None else Thumbnail(row.data, row.format, row.width, row.height)

    def put(self, key: str, size: int, thumbnail: Thumbnail) -> None:
        """Store ``thumbnail`` under ``key`` (replacing what was there)."""
        values = {
            "key": key,
            "size": size,
            "format": thumbnail.format,
            "width": thumbnail.width,
            "height": thumbnail.height,
            "data": thumbnail.data,
            "created_at": utcnow().replace(tzinfo=None),
        }
        with self._lock:
            engine = self._open()
            with engine.begin() as conn:
                statement = insert(thumb).values(values)
                conn.execute(
                    statement.on_conflict_do_update(
                        index_elements=[thumb.c.key],
                        set_={k: statement.excluded[k] for k in values if k != "key"},
                    )
                )

    def clear(self) -> int:
        """Remove every thumbnail; returns how many there were. The file shrinks too."""
        with self._lock:
            engine = self._open()
            with engine.begin() as conn:
                removed = conn.execute(delete(thumb)).rowcount
            with engine.connect() as conn:
                conn.execute(text("VACUUM"))
        logger.info("Cleared %d thumbnails from %s", removed, self.path)
        return int(removed)

    def stats(self) -> CacheStats:
        """How many thumbnails are stored, and their total size in bytes."""
        with self._lock:
            engine = self._open()
            with engine.connect() as conn:
                count, size = conn.execute(
                    select(func.count(), func.coalesce(func.sum(func.length(thumb.c.data)), 0))
                ).one()
        return CacheStats(int(count), int(size))

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None

    # --- the file ---

    def _open(self) -> Engine:
        """The engine, opening (or creating, or recreating) ``thumbs.db`` on first use."""
        if self._engine is not None:
            return self._engine
        try:
            self._engine = self._connect()
        except (DatabaseError, _OtherVersion) as e:
            logger.warning("%s is unreadable (%s); starting it afresh", self.path, e)
            self._discard()
            self._engine = self._connect()
        return self._engine

    def _connect(self) -> Engine:
        # One connection, shared under the lock: thumbnails are written one at a time.
        engine = create_engine(
            URL.create("sqlite+pysqlite", database=str(self.path)),
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute("PRAGMA synchronous = OFF")  # a cache: losing it is fine
            finally:
                cursor.close()

        try:
            with engine.begin() as conn:
                version: int = conn.execute(text("PRAGMA user_version")).scalar_one()
                if version != CACHE_VERSION and inspect(conn).get_table_names():
                    raise _OtherVersion(f"format {version}, expected {CACHE_VERSION}")
                metadata.create_all(conn)
                conn.execute(text(f"PRAGMA user_version = {CACHE_VERSION}"))
        except BaseException:
            engine.dispose()
            raise
        return engine

    def _discard(self) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
