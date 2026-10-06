"""Documents' text, for searching inside them (DESIGN.md §8 *Search inside documents*).

The text lives in ``fulltext.db`` beside ``keep.db`` (:class:`ContentsStore`), so the main
database stays small and this one can be deleted and rebuilt. It has its own writer thread:
the one-writer rule is per database.

- **Which files:** those in the primary role of the theme's ``full_text`` types
  (:func:`document_files`), read when new or changed since they were last read (size and
  modification time, :func:`files_to_read`).
- **Reading** (:class:`ContentsQueue`): in the background after a scan, :data:`THREADS` at a
  time, by ``Theme.document_text`` or else :func:`~tagalot.themes.api.read_document_text`,
  as pages. A file that can't be read is recorded with its error and not tried again until
  it changes. Nothing is read while the keep's contents search is Off.
- A file whose resource is deleted loses its text (:func:`forget_deleted`); one that is
  missing or offline keeps it (AGENTS.md rule 7).
"""

import logging
import threading
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import (
    Column,
    Connection,
    Integer,
    MetaData,
    Table,
    Text,
    delete,
    func,
    insert,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core.db import create_keep_engine
from tagalot.core.models import (
    Entity,
    EntityResource,
    Resource,
    ResourceKind,
    ResourceStatus,
    UTCDateTime,
    utcnow,
)
from tagalot.core.roots import local_path
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import ResourceInfo, Theme, read_document_text

logger = logging.getLogger(__name__)

FULLTEXT_DB = "fulltext.db"
SCHEMA_VERSION = 1
"""``PRAGMA user_version`` of ``fulltext.db``; a file with another version is rebuilt (it
holds nothing that can't be read again)."""
THREADS = 2
"""Files read at once in the background (a PDF's pages take PDFium's lock in turn)."""
BATCH = 20
"""Files saved in one write."""

metadata = MetaData()
contents_file = Table(
    "contents_file",
    metadata,
    Column("resource_id", Integer, primary_key=True),
    Column("size", Integer),
    Column("mtime_ns", Integer),
    Column("read_at", UTCDateTime, nullable=False),
    Column("pages", Integer, nullable=False, default=0),
    Column("error", Text),
)
"""One row per file read: its size and modification time when read (a change means it is
read again), how many pages it gave, and why it couldn't be read, if it couldn't."""
contents_page = Table(
    "contents_page",
    metadata,
    Column("resource_id", Integer, primary_key=True),
    Column("page", Integer, primary_key=True),
    Column("text", Text, nullable=False),
)
"""A file's text, a row per page (counting from 1): a PDF's page, an EPUB's chapter."""


class ContentsStore:
    """``fulltext.db``: a writer and a reader for it. Opened when first needed."""

    def __init__(self, path: Path, *, network: bool = False) -> None:
        self.path = path
        engine = create_keep_engine(path, network=network)
        with engine.begin() as conn:
            version = conn.exec_driver_sql("PRAGMA user_version").scalar()
            if version not in (0, SCHEMA_VERSION):
                logger.warning("%s: format %s, rebuilding it", path, version)
                metadata.drop_all(conn)
            metadata.create_all(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.writer = DbWriter(engine, name="contents-writer")
        self.reader = create_keep_engine(path, network=network, read_only=True)

    def close(self) -> None:
        self.writer.close()
        self.reader.dispose()


@dataclass(frozen=True)
class DocumentFile:
    """A document file to read."""

    resource_id: int
    root_id: str
    relpath: str
    ext: str
    size: int | None
    mtime_ns: int | None


def full_text_types(schema: ThemeSchema) -> dict[str, str]:
    """The theme's ``full_text`` types: type id -> its primary role."""
    found: dict[str, str] = {}
    for entity_table in schema.entities.values():
        entity = entity_table.entity
        primary = next((r.name for r in entity.roles if r.primary), None)
        if getattr(entity, "full_text", False) and primary is not None:
            found[entity_table.type_id] = primary
    return found


def document_files(conn: Connection, schema: ThemeSchema) -> list[DocumentFile]:
    """The files in the primary role of the theme's ``full_text`` types that are there to
    read (present, not skipped), newest first."""
    files: dict[int, DocumentFile] = {}
    for type_id, role in full_text_types(schema).items():
        rows = conn.execute(
            select(
                Resource.id,
                Resource.root_id,
                Resource.relpath,
                Resource.ext,
                Resource.size,
                Resource.mtime_ns,
            )
            .join(EntityResource, EntityResource.resource_id == Resource.id)
            .join(Entity, Entity.id == EntityResource.entity_id)
            .where(
                Entity.type == type_id,
                EntityResource.role == role,
                Resource.kind == ResourceKind.FILE,
                Resource.status == ResourceStatus.OK,
                Resource.skipped.is_(False),
                Resource.parent_resource_id.is_(None),
            )
        )
        for row in rows:
            files[row.id] = DocumentFile(*row)
    return [files[k] for k in sorted(files, reverse=True)]


def files_to_read(keep: Connection, store: Connection, schema: ThemeSchema) -> list[DocumentFile]:
    """The document files not read yet, or changed since (size or modification time)."""
    known = {
        row.resource_id: (row.size, row.mtime_ns)
        for row in store.execute(
            select(contents_file.c.resource_id, contents_file.c.size, contents_file.c.mtime_ns)
        )
    }
    return [
        f for f in document_files(keep, schema) if known.get(f.resource_id) != (f.size, f.mtime_ns)
    ]


def forget_deleted(keep: Connection, store_writer: DbWriter) -> int:
    """Drop the text of files whose resources were deleted; returns how many."""
    existing = set(keep.scalars(select(Resource.id)))

    def job(conn: Connection) -> int:
        stored: set[int] = set(conn.scalars(select(contents_file.c.resource_id)))
        gone = sorted(stored - existing)
        for start in range(0, len(gone), 500):
            batch = gone[start : start + 500]
            conn.execute(delete(contents_page).where(contents_page.c.resource_id.in_(batch)))
            conn.execute(delete(contents_file).where(contents_file.c.resource_id.in_(batch)))
        return len(gone)

    return store_writer.run(job)


@dataclass(frozen=True)
class FileText:
    """What reading one file gave."""

    file: DocumentFile
    pages: Sequence[str]
    error: str | None = None


def save_texts(conn: Connection, texts: Iterable[FileText]) -> None:
    """Replace these files' text (in the contents writer)."""
    for found in texts:
        rid = found.file.resource_id
        conn.execute(delete(contents_page).where(contents_page.c.resource_id == rid))
        rows = [
            {"resource_id": rid, "page": n, "text": text}
            for n, text in enumerate(found.pages, start=1)
        ]
        if rows:
            conn.execute(insert(contents_page), rows)
        upsert = sqlite_insert(contents_file).values(
            resource_id=rid,
            size=found.file.size,
            mtime_ns=found.file.mtime_ns,
            read_at=utcnow(),
            pages=len(rows),
            error=found.error,
        )
        conn.execute(
            upsert.on_conflict_do_update(
                index_elements=["resource_id"],
                set_={
                    "size": upsert.excluded.size,
                    "mtime_ns": upsert.excluded.mtime_ns,
                    "read_at": upsert.excluded.read_at,
                    "pages": upsert.excluded.pages,
                    "error": upsert.excluded.error,
                },
            )
        )


def read_file(theme: Theme, file: DocumentFile, path: str) -> FileText:
    """Read one file's text: the theme's way, else the core's. Never raises: a file that
    can't be read gives its error."""
    resource = ResourceInfo(
        file.resource_id,
        file.root_id,
        file.relpath,
        "file",
        file.ext,
        file.size,
        file.mtime_ns,
        path,
    )
    try:
        pages = theme.document_text(resource)
        if pages is None:
            pages = read_document_text(path)
        return FileText(file, list(pages))
    except Exception as e:  # a bad file, or a theme bug: reported, the rest go on
        if not isinstance(e, (OSError, ValueError)):
            logger.exception("Reading the text of %s failed", file.relpath)
        return FileText(file, [], f"{type(e).__name__}: {e}")


@dataclass
class ContentsResult:
    """What a run of :class:`ContentsQueue` did."""

    read: int = 0
    pages: int = 0
    failed: list[tuple[str, str, str]] = field(default_factory=list)
    """``(root id, relpath, message)`` of files that couldn't be read."""
    stopped: bool = False


Progress = Callable[[int, int], None]
"""``progress(done, total)``, from the queue's thread."""


class ContentsQueue:
    """Reads a list of document files in the background, saving their text a batch at a
    time. Starting again replaces the current run; :meth:`close` stops for good."""

    def __init__(
        self, store: ContentsStore, theme: Theme, root_path: Callable[[str], str | None]
    ) -> None:
        self.store = store
        self.theme = theme
        self.root_path = root_path
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
        files: Sequence[DocumentFile],
        progress: Progress | None = None,
        done: Callable[[ContentsResult], None] | None = None,
    ) -> None:
        self.stop()
        stop = threading.Event()
        with self._lock:
            if self._closed:
                return
            self._stop = stop
            self.remaining = len(files)
            self._thread = threading.Thread(
                target=self._run,
                args=(list(files), stop, progress, done),
                name="contents-queue",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the current run and wait for it (at most the files being read)."""
        with self._lock:
            thread, self._thread = self._thread, None
            self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop()

    def _path(self, file: DocumentFile) -> str | None:
        root = self.root_path(file.root_id)
        return local_path(root, file.relpath) if root else None

    def _run(
        self,
        files: list[DocumentFile],
        stop: threading.Event,
        progress: Progress | None,
        done: Callable[[ContentsResult], None] | None,
    ) -> None:
        result = ContentsResult()
        total = len(files)
        finished = 0
        batch: list[FileText] = []

        def save() -> None:
            if batch:
                texts = list(batch)
                batch.clear()
                self.store.writer.run(lambda conn: save_texts(conn, texts))

        def finish(future: Future[FileText | None]) -> None:
            nonlocal finished
            found = future.result()
            finished += 1
            self.remaining = total - finished
            if found is not None:
                batch.append(found)
                if found.error:
                    result.failed.append((found.file.root_id, found.file.relpath, found.error))
                else:
                    result.read += 1
                    result.pages += len(found.pages)
                if len(batch) >= BATCH:
                    save()
            if progress is not None:
                _tell(progress, finished, total)

        def read(file: DocumentFile) -> FileText | None:
            if stop.is_set():
                return None
            path = self._path(file)
            return read_file(self.theme, file, path) if path else None

        pool = ThreadPoolExecutor(THREADS, thread_name_prefix="contents-queue")
        window: deque[Future[FileText | None]] = deque()
        try:
            for file in files:
                if stop.is_set():
                    break
                window.append(pool.submit(read, file))
                if len(window) >= THREADS * 2:
                    finish(window.popleft())
            while window and not stop.is_set():
                finish(window.popleft())
            save()  # what was read is kept, even when stopping
        except Exception:
            logger.exception("Reading documents' text failed")
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        self.remaining = 0
        result.stopped = finished < total
        if done is not None and not stop.is_set():
            _tell(done, result)


def page_count(conn: Connection) -> int:
    """How many pages of text ``fulltext.db`` holds."""
    return int(conn.scalar(select(func.count()).select_from(contents_page)) or 0)


def _tell(callback: Callable[..., None], *args: object) -> None:
    try:
        callback(*args)
    except Exception:
        logger.debug("Contents queue: report not delivered", exc_info=True)
