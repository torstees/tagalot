"""An open keep: everything the UI needs, opened and closed in the right order.

``KeepSession.open`` does file and database I/O (and may migrate), so the UI calls it from a
worker. It never imports Qt.
"""

import logging
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, and_, or_, select, update
from sqlalchemy.exc import OperationalError

from tagalot.core.actions import ActionResult, delete_items, run_action
from tagalot.core.activity import (
    ProblemLog,
    contents_problems,
    online_problems,
    problems_from_report,
    thumbnail_problems,
)
from tagalot.core.containers import contain_by_hand, new_container, uncontain_by_hand
from tagalot.core.contents import (
    FULLTEXT_DB,
    INDEXES,
    ContentsQueue,
    ContentsResult,
    ContentsStats,
    ContentsStore,
    attach_on_connect,
    attached_meta,
    build_index,
    clear_contents,
    contents_stats,
    drop_indexes,
    files_to_read,
    forget_deleted,
    full_text_types,
    ready_index,
)
from tagalot.core.contents import Progress as ContentsProgress
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.dedupe import Verifier
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import Keep, KeepConfig, open_keep, save_keep_config
from tagalot.core.keep_settings import (
    with_contents_index,
    with_name,
    with_online_lookups,
    with_option,
    with_thumbnail_max,
    with_thumbnails_after_scan,
)
from tagalot.core.keywords import any_keywords
from tagalot.core.links import link_files, unlink_file
from tagalot.core.merge import MergePlan, merge_items, plan_merge
from tagalot.core.models import Entity, EntityResource, Root
from tagalot.core.not_duplicates import Entry, set_not_duplicate
from tagalot.core.online import (
    Fetcher,
    LookupReport,
    NotAllowedError,
    Opener,
    changed_since,
    look_up,
    to_fetch,
    urlopen_reply,
)
from tagalot.core.online import Progress as OnlineProgress
from tagalot.core.reextract import ReextractReport, reextract
from tagalot.core.relations import add_related, move_related, remove_related
from tagalot.core.root_admin import (
    RemovalCounts,
    delete_root,
    edit_root,
    unwatch,
    without_root,
)
from tagalot.core.roots import sync_roots
from tagalot.core.saved_searches import (
    SavedDefinition,
    delete_search,
    rename_search,
    save_search,
)
from tagalot.core.scanjob import Progress, ScanReport, scan_root
from tagalot.core.search import ContentsScope
from tagalot.core.search_spec import SearchSpec
from tagalot.core.settings import Settings
from tagalot.core.tag_service import TagService
from tagalot.core.tags import TagTreeCache
from tagalot.core.theme_db import open_theme, resolve_theme
from tagalot.core.theme_schema import ThemeSchema, build_theme_schema
from tagalot.core.thumbnails.cache import ThumbCache
from tagalot.core.thumbnails.queue import (
    Progress as QueueProgress,
)
from tagalot.core.thumbnails.queue import (
    QueueResult,
    ThumbnailQueue,
    entities_needing_thumbnails,
)
from tagalot.core.thumbnails.resolve import ThumbnailResolver
from tagalot.core.triage import dismiss
from tagalot.core.writer import DbWriter
from tagalot.themes.api import Theme
from tagalot.themes.loader import ThemeCatalog, load_themes

logger = logging.getLogger(__name__)


@dataclass
class KeepSession:
    """An open keep with its writer, read-only engine, theme, and tag services."""

    keep: Keep
    theme: type[Theme]
    schema: ThemeSchema
    writer: DbWriter
    reader: Engine
    tag_cache: TagTreeCache
    tags: TagService
    settings: Settings
    catalog: ThemeCatalog
    thumbnails: ThumbnailResolver
    """Resolves and caches thumbnails (``thumbs.db``) at :attr:`thumbnail_max`."""
    closed: bool = field(default=False, init=False)
    """True once :meth:`close` has finished: the database files are no longer open."""
    _closing: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _temp: Path | None = field(default=None, init=False, repr=False)
    _queue: ThumbnailQueue | None = field(default=None, init=False, repr=False)
    problems: ProblemLog = field(default_factory=ProblemLog, init=False)
    verifier: Verifier = field(default_factory=Verifier, init=False)
    """Full-hash checks of duplicate files, kept for the session (core.dedupe)."""
    """What went wrong while the keep was open: scans and thumbnails (the activity panel)."""
    last_scan_started: datetime | None = field(default=None, init=False)
    has_keywords: bool = field(default=False, init=False)
    """Some file has given an item keywords (§7): lists then offer a Keywords column.
    Read at open; the window updates it when it recounts keywords."""
    """When the latest :meth:`scan_all` began (its thumbnails are queued after it)."""
    _temp_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    online_opener: Opener = field(default_factory=lambda: urlopen_reply, init=False, repr=False)
    """How lookups fetch an address (tests answer from a table instead)."""
    _contents: ContentsStore | None = field(default=None, init=False, repr=False)
    _contents_queue: ContentsQueue | None = field(default=None, init=False, repr=False)
    _closing_lookups: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )

    @classmethod
    def open(
        cls,
        keep_dir: Path,
        settings: Settings,
        *,
        catalog: ThemeCatalog | None = None,
        allow_migration: bool = False,
    ) -> "KeepSession":
        """Open a keep: ``keep.toml``, the core schema, the theme, then the services.

        Raises the :class:`~tagalot.core.keep.KeepError` family (not a keep, newer version,
        needs migration, theme unavailable…) with messages meant for the user. Nothing is
        left open if it fails.
        """
        keep = open_keep(keep_dir)
        catalog = catalog or load_themes(extra_dirs=settings.theme_dirs)
        theme = resolve_theme(catalog, keep)
        schema = build_theme_schema(theme)  # catalog themes passed a trial build already
        keep, engine = open_keep_database(keep, allow_migration=allow_migration)
        try:
            opened = open_theme(
                engine,
                keep,
                theme,
                allow_migration=allow_migration,
                make_context=lambda conn: IngestSession(conn, schema),
            )
        except BaseException:
            engine.dispose()
            raise
        writer = DbWriter(engine)
        reader = create_keep_engine(opened.keep.db_path, network=keep.on_network, read_only=True)
        attach_on_connect(reader, opened.keep.dir / FULLTEXT_DB)  # searching inside documents
        cache = TagTreeCache(reader)
        thumbnail_max = opened.keep.config.thumbnail_max or theme.thumbnail_max
        root_paths = {
            r.id: settings.root_path(opened.keep.config.id, r) for r in opened.keep.config.roots
        }
        thumbnails = ThumbnailResolver(
            reader,
            writer,
            theme,
            ThumbCache(opened.keep.thumbs_path),
            root_paths.__getitem__,
            thumbnail_max,
        )
        logger.info("Opened keep %r with the %r theme", opened.keep.config.name, theme.id)
        session = cls(
            keep=opened.keep,
            theme=theme,
            schema=opened.schema,
            writer=writer,
            reader=reader,
            tag_cache=cache,
            tags=TagService(writer, cache, opened.schema),
            settings=settings,
            catalog=catalog,
            thumbnails=thumbnails,
        )
        # From now on, root paths follow the configuration (roots added, moved, overridden).
        thumbnails.root_path = session.root_path
        thumbnails.report = lambda found: session.problems.add(thumbnail_problems(found))
        with reader.connect() as conn:
            session.has_keywords = any_keywords(conn)
        return session

    @property
    def thumbnail_max(self) -> int:
        """The size thumbnails are made at: the keep's override, else the theme's."""
        return self.thumbnails.size

    @property
    def thumbnail_default(self) -> int:
        """The theme's starting size for grid cards (never above :attr:`thumbnail_max`)."""
        return min(self.theme.thumbnail_default, self.thumbnail_max)

    def root_path(self, root_id: str) -> str:
        """This machine's path for a root (per-user override, else ``keep.toml``)."""
        root = next(r for r in self.keep.config.roots if r.id == root_id)
        return self.settings.root_path(self.keep.config.id, root)

    def scan_all(
        self, progress: Progress | None = None, root_ids: Iterable[str] | None = None
    ) -> list[ScanReport]:
        """Scan every watched root (or those of ``root_ids``) in turn with the keep's
        theme. Runs in a worker."""
        reports = []
        self.last_scan_started = datetime.now(UTC)
        wanted = set(root_ids) if root_ids is not None else None
        for root in self.keep.config.roots:
            if not root.watched or (wanted is not None and root.id not in wanted):
                continue
            reports.append(
                scan_root(
                    self.writer,
                    self.reader,
                    root,
                    self.root_path(root.id),
                    theme=self.theme,
                    schema=self.schema,
                    theme_options=self.keep.config.theme_options,
                    progress=progress,
                )
            )
            self.problems.add(problems_from_report(reports[-1]))
        return reports

    def reextract(
        self, entity_ids: Iterable[int], *, replace_edits: bool = False
    ) -> ReextractReport:
        """Read these entities' files again, now; one undo step if anything changed. With
        ``replace_edits``, the files' values replace what the user edited. Runs in a worker."""
        report = reextract(
            self.writer,
            self.reader,
            self.schema,
            self.theme,
            self.keep.config.roots,
            self.root_path,
            entity_ids,
            replace_edits=replace_edits,
            theme_options=self.keep.config.theme_options,
        )
        change = report.change
        if change.before != change.after:
            self.tags.record(change)
        self.thumbnails.forget_failures()  # files may be readable again
        return report

    def look_up(
        self,
        entity_ids: Iterable[int],
        *,
        refresh: bool = False,
        progress: OnlineProgress | None = None,
        stopped: Callable[[], bool] = lambda: False,
        fetcher: Fetcher | None = None,
    ) -> LookupReport:
        """Look these items' details up online (§9 *Online details*): only when the keep
        allows it (else :class:`~tagalot.core.online.NotAllowedError`), and only what has
        no kept answer unless ``refresh``. Problems go to the activity panel. Runs in a
        worker."""
        if self.keep.config.online_lookups != "allow":
            raise NotAllowedError("Online lookups aren't allowed for this keep.")
        closing = self._closing_lookups

        def stop() -> bool:
            return closing.is_set() or stopped()

        theme = self.theme()
        fetcher = fetcher or Fetcher(theme.online_sources, opener=self.online_opener, stopped=stop)
        report = look_up(
            self.writer,
            self.reader,
            self.schema,
            theme,
            entity_ids,
            refresh=refresh,
            fetcher=fetcher,
            progress=progress,
            stopped=stop,
        )
        self.problems.add(online_problems(report.problems))
        return report

    # --- searching inside documents (core.contents) ---

    @property
    def contents(self) -> ContentsStore:
        """``fulltext.db``, opened when first needed."""
        with self._temp_lock:
            if self._contents is None:
                self._contents = ContentsStore(
                    self.keep.dir / FULLTEXT_DB, network=self.keep.on_network
                )
            return self._contents

    @property
    def contents_queue(self) -> ContentsQueue:
        store = self.contents
        with self._temp_lock:
            if self._contents_queue is None:
                self._contents_queue = ContentsQueue(store, self.theme(), self._known_root_path)
            return self._contents_queue

    def queue_contents(
        self,
        progress: ContentsProgress | None = None,
        done: Callable[[ContentsResult], None] | None = None,
    ) -> int:
        """Read the document files that are new or changed since they were last read, in
        the background (when the keep searches inside documents); returns how many. Files
        that can't be read go to the activity panel. Runs in a worker."""
        if self.keep.config.contents_index is None or not full_text_types(self.schema):
            return 0
        store = self.contents
        with self.reader.connect() as keep:
            forget_deleted(keep, store.writer)
            with store.reader.connect() as conn:
                files = files_to_read(keep, conn, self.schema)
        if not files:
            return 0
        problems = self.problems

        def finished(result: ContentsResult) -> None:
            problems.add(contents_problems(result.failed))
            if done is not None:
                done(result)

        self.contents_queue.start(files, progress, finished)
        return len(files)

    def set_contents_index(self, index: str | None) -> None:
        """Search inside documents with a Words or Substrings index, or not (``None``).
        Turning it off stops reading and drops the index (the text stays). Turning it on
        or switching only saves the choice: :meth:`ensure_contents_index` builds the index.
        Runs in a worker."""
        self.save_config(with_contents_index(self.keep.config, index))
        if index is None:
            if self._contents_queue is not None:
                self._contents_queue.stop()
            if self._contents is not None or (self.keep.dir / FULLTEXT_DB).exists():
                self.contents.writer.run(drop_indexes)

    @property
    def has_documents(self) -> bool:
        """The theme has document types (``full_text``): contents search applies."""
        return bool(full_text_types(self.schema))

    def ensure_contents_index(self) -> bool:
        """Build the index the keep asks for, from the stored text, if it isn't the one
        built (turned on, switched, or ``keep.toml`` edited); returns whether it built one.
        Searches use the old index until the new one is ready. Runs in a worker."""
        wanted = self.keep.config.contents_index
        if wanted is None or not self.has_documents:
            return False
        store = self.contents
        with store.reader.connect() as conn:
            if ready_index(conn) == wanted:
                return False
        store.writer.run(lambda conn: build_index(conn, wanted))
        return True

    def contents_scope(self, conn: Connection, spec: SearchSpec) -> ContentsScope | None:
        """What ``spec`` needs to match inside documents, on a connection of :attr:`reader`
        (which has ``fulltext.db`` attached): ``None`` unless the spec asks for it, the keep
        searches contents, and an index is built."""
        types = full_text_types(self.schema)
        if not spec.contents or self.keep.config.contents_index is None or not types:
            return None
        if not (self.keep.dir / FULLTEXT_DB).exists():
            return None
        try:
            index = conn.scalar(select(attached_meta.c.value).where(attached_meta.c.key == "index"))
        except OperationalError:  # attached before fulltext.db was made: not this time
            return None
        if index not in INDEXES:
            return None
        files = or_(
            *(
                and_(Entity.type == type_id, EntityResource.role == role)
                for type_id, role in types.items()
            )
        )
        return ContentsScope(index, files)

    def clear_contents(self) -> None:
        """**Clear contents text…**: turn contents search off and delete every document's
        text and the index. Runs in a worker."""
        self.set_contents_index(None)
        self.contents.writer.run(clear_contents)

    def contents_stats(self) -> ContentsStats | None:
        """What ``fulltext.db`` holds (``None``: there is none). Runs in a worker."""
        if self._contents is None and not (self.keep.dir / FULLTEXT_DB).exists():
            return None
        store = self.contents
        with store.reader.connect() as conn:
            return contents_stats(conn, store.path)

    def lookup_candidates(self, since: datetime | None) -> list[int]:
        """Items to look up after a scan that began at ``since`` (those it made or
        changed), or every item (``None``). Runs in a worker."""
        if not self.theme.online_sources:
            return []
        with self.reader.connect() as conn:
            return changed_since(conn, since)

    def to_fetch(self, entity_ids: Iterable[int], *, refresh: bool = False) -> int:
        """How many of these items a lookup would fetch something for. Runs in a worker."""
        with self.reader.connect() as conn:
            return to_fetch(conn, self.schema, self.theme(), entity_ids, refresh=refresh)

    def run_action(self, method: str, entity_ids: Iterable[int]) -> ActionResult:
        """Run a theme action on these entities in the DB writer; one undo step if it
        changed anything. Runs in a worker; raises ``ActionError`` if the action fails (its
        writes are then rolled back)."""
        ids = list(entity_ids)
        theme = self.theme()
        schema = self.schema

        def job(conn: Connection) -> ActionResult:
            return run_action(
                conn,
                schema,
                theme,
                method,
                ids,
                root_path=self._known_root_path,
                temp_dir=self.temp_dir,
            )

        result = self.writer.run(job)
        if result.changed:
            self.tags.record(result.change)
        return result

    # --- roots (the Keep configuration window; core.root_admin) ---

    def save_config(self, config: KeepConfig) -> None:
        """Write a changed ``keep.toml`` (checked by ``root_admin`` first) and bring the
        ``root`` table in line. Runs in a worker."""
        save_keep_config(config, self.keep.toml_path)
        self.keep = replace(self.keep, config=config)
        self.writer.run(lambda conn: sync_roots(conn, config.roots))

    # --- links made by hand (core.links) ---

    def link_files(self, entity_id: int, resource_ids: Sequence[int], role: str) -> str:
        """Link files to an item by hand; one undo step. Returns its label. Raises
        ``LinkError`` for a link that can't be made. Runs in a worker."""
        schema = self.schema
        change = self.writer.run(
            lambda conn: link_files(conn, schema, entity_id, resource_ids, role)
        )
        self.tags.record(change)
        return change.label

    def plan_merge(self, keep_id: int, other_ids: Sequence[int]) -> MergePlan:
        """What merging these items would do (for the dialog). Raises ``MergeError``.
        Runs in a worker."""
        with self.reader.connect() as conn:
            return plan_merge(conn, self.schema, keep_id, other_ids)

    def merge_items(
        self, keep_id: int, other_ids: Sequence[int], choices: Mapping[str, int]
    ) -> str:
        """Merge items into one (§13); one undo step. Returns its label. Raises
        ``MergeError``. Runs in a worker."""
        schema = self.schema
        change = self.writer.run(
            lambda conn: merge_items(conn, schema, keep_id, other_ids, choices)
        )
        self.tags.record(change)
        return change.label

    def set_not_duplicate(self, entries: Sequence[Entry], dismissed: bool = True) -> str:
        """Hide groups or pairs from Dedupe as not duplicates (or show them again); one
        undo step. Returns its label. Runs in a worker."""
        change = self.writer.run(lambda conn: set_not_duplicate(conn, entries, dismissed=dismissed))
        self.tags.record(change)
        return change.label

    def add_related(
        self,
        entity_id: int,
        name: str,
        other_id: int | None,
        new_title: str | None = None,
        side: str | None = None,
    ) -> str:
        """Relate an item (or a new one named ``new_title``) by hand; one undo step.
        Returns its label. Raises ``RelationError``. Runs in a worker."""
        schema = self.schema
        change = self.writer.run(
            lambda conn: add_related(
                conn, schema, name, entity_id, other_id, new_title=new_title, side=side
            )
        )
        self.tags.record(change)
        return change.label

    def remove_related(
        self, entity_id: int, name: str, other_ids: Sequence[int], side: str | None = None
    ) -> str:
        """Unrelate items by hand (remembered, so scans don't relate them again); one undo
        step. Returns its label. Runs in a worker."""
        schema = self.schema
        change = self.writer.run(
            lambda conn: remove_related(conn, schema, name, entity_id, other_ids, side)
        )
        self.tags.record(change)
        return change.label

    def new_container(self, type_id: str, title: str) -> tuple[str, int]:
        """Make an item of a hand-made type (a project, #329); one undo step. Returns its
        label and the new item's id. Raises ``ContainerError``. Runs in a worker."""
        schema = self.schema
        change, entity_id = self.writer.run(
            lambda conn: new_container(conn, schema, type_id, title)
        )
        self.tags.record(change)
        return change.label, entity_id

    def contain_by_hand(self, parent_id: int, child_ids: Sequence[int]) -> str:
        """Put items in a hand-made container; one undo step. Returns its label."""
        schema = self.schema
        change = self.writer.run(lambda conn: contain_by_hand(conn, schema, parent_id, child_ids))
        self.tags.record(change)
        return change.label

    def uncontain_by_hand(self, parent_id: int, child_ids: Sequence[int]) -> str:
        """Take items out of a container by hand; one undo step. Returns its label."""
        schema = self.schema
        change = self.writer.run(lambda conn: uncontain_by_hand(conn, schema, parent_id, child_ids))
        self.tags.record(change)
        return change.label

    def move_related(self, b_id: int, name: str, a_ids: Sequence[int], by: int) -> str:
        """Move items up (``by`` < 0) or down among ``b_id``'s items of an ordered
        relationship (a paper's authors, #317); remembered, so scans keep the order. One
        undo step. Returns its label. Raises ``RelationError``. Runs in a worker."""
        schema = self.schema
        change = self.writer.run(lambda conn: move_related(conn, schema, name, b_id, a_ids, by))
        self.tags.record(change)
        return change.label

    def save_search(
        self, name: str, definition: SavedDefinition, replace: int | None = None
    ) -> str:
        """Save a search (over ``replace``, if given); one undo step. Returns its label.
        Raises ``SavedSearchError``. Runs in a worker."""
        change = self.writer.run(lambda conn: save_search(conn, name, definition, replace=replace))
        self.tags.record(change)
        return change.label

    def rename_saved(self, saved_id: int, name: str) -> str:
        change = self.writer.run(lambda conn: rename_search(conn, saved_id, name))
        self.tags.record(change)
        return change.label

    def delete_saved(self, saved_id: int) -> str:
        change = self.writer.run(lambda conn: delete_search(conn, saved_id))
        self.tags.record(change)
        return change.label

    def unlink_file(self, entity_id: int, resource_id: int, role: str) -> str:
        """Remove a link made by hand; one undo step. Runs in a worker."""
        schema = self.schema
        change = self.writer.run(
            lambda conn: unlink_file(conn, schema, entity_id, resource_id, role)
        )
        self.tags.record(change)
        return change.label

    # --- triage (core.triage) ---

    def dismiss(self, name: str, ids: Sequence[int]) -> int:
        """Hide files or items from a triage list until they change; one undo step.
        Returns how many. Runs in a worker."""
        change = self.writer.run(lambda conn: dismiss(conn, name, ids))
        if change.before != change.after:
            self.tags.record(change)
        return len(change.after)

    def delete_items(self, ids: Sequence[int]) -> int:
        """Delete items (never their files); one undo step. Runs in a worker."""
        schema = self.schema
        change = self.writer.run(lambda conn: delete_items(conn, schema, ids))
        if change.before != change.after:
            self.tags.record(change)
        return sum(1 for state in change.before.values() if state is not None) - sum(
            1 for state in change.after.values() if state is not None
        )

    # --- background thumbnails (core.thumbnails.queue) ---

    @property
    def thumbnail_queue(self) -> ThumbnailQueue:
        with self._temp_lock:  # created once, whichever thread asks first
            if self._queue is None:
                self._queue = ThumbnailQueue(self.thumbnails)
            return self._queue

    def queue_thumbnails(
        self,
        progress: QueueProgress | None = None,
        done: Callable[[QueueResult], None] | None = None,
    ) -> int:
        """Make the latest scan's thumbnails in the background (if the keep does that);
        returns how many entities were queued. Runs in a worker (it reads the database)."""
        if not self.keep.config.thumbnails_after_scan or self.closed:
            return 0
        with self.reader.connect() as conn:
            ids = entities_needing_thumbnails(conn, self.last_scan_started)
        if ids:
            self.thumbnail_queue.start(ids, progress, done)
        return len(ids)

    def set_thumbnails_after_scan(self, on: bool) -> None:
        """Turn background thumbnails after scans on or off; off stops a run."""
        self.save_config(with_thumbnails_after_scan(self.keep.config, on))
        if not on and self._queue is not None:
            self._queue.stop()

    def set_online_lookups(self, answer: str | None) -> None:
        """Allow (``"allow"``) or refuse (``"never"``) online lookups for this keep, or
        forget the answer (``None``: ask again). Runs in a worker."""
        self.save_config(with_online_lookups(self.keep.config, answer))

    def rename_keep(self, name: str) -> None:
        """Rename the keep. Runs in a worker."""
        self.save_config(with_name(self.keep.config, self.keep.dir, name))

    def set_thumbnail_max(self, size: int | None) -> None:
        """Make thumbnails at ``size`` pixels from now on (``None``: the theme's size);
        cached ones at another size are made again as they're shown. Runs in a worker."""
        self.save_config(with_thumbnail_max(self.keep.config, size))
        self.thumbnails.size = size or self.theme.thumbnail_max

    def set_option(self, name: str, value: Any, root_id: str | None = None) -> None:
        """Set a theme option for the keep or one root (``None`` removes the setting).
        It applies at the next scan, which reads the affected roots' files again."""
        self.save_config(with_option(self.keep.config, self.theme, name, value, root_id=root_id))

    def stop_watching(self, root_id: str) -> None:
        """Stop scanning a root; its items stay, shown offline. Runs in a worker."""
        self.save_config(edit_root(self.keep.config, self.keep.dir, root_id, watched=False))
        self.writer.run(lambda conn: unwatch(conn, root_id))

    def watch_again(self, root_id: str) -> None:
        """Scan a root again from now on (the next scan reconnects its items)."""
        self.save_config(edit_root(self.keep.config, self.keep.dir, root_id, watched=True))
        self.writer.run(
            lambda conn: conn.execute(
                update(Root).where(Root.id == root_id).values(last_error=None)
            )
        )

    def delete_root(self, root_id: str) -> RemovalCounts:
        """Remove a root with its items (never its files on disk). Runs in a worker."""
        self.save_config(without_root(self.keep.config, root_id))
        schema = self.schema
        counts = self.writer.run(lambda conn: delete_root(conn, schema, root_id))
        self.settings.set_root_override(self.keep.config.id, root_id, None)
        return counts

    def temp_dir(self) -> Path:
        """A folder of this session's own for files actions write (a playlist); it is
        deleted when the keep closes."""
        with self._temp_lock:  # not _closing: close() waits for writer jobs that call this
            if self._temp is None:
                self._temp = Path(tempfile.mkdtemp(prefix="tagalot-"))
            return self._temp

    def _known_root_path(self, root_id: str) -> str | None:
        try:
            return self.root_path(root_id)
        except StopIteration:  # a root no longer in keep.toml
            return None

    def close(self) -> None:
        """Finish queued writes and release the database. Safe to call twice, and from two
        threads: the second call waits for the first to finish."""
        with self._closing:
            if self.closed:
                return
            self._closing_lookups.set()  # a lookup stops before its next request
            self.thumbnail_queue.close()  # before the database it reads goes
            if self._contents_queue is not None:
                self._contents_queue.close()
            if self._contents is not None:
                self._contents.close()
            self.writer.close()
            if self._temp is not None:
                shutil.rmtree(self._temp, ignore_errors=True)
            self.thumbnails.cache.close()
            self.reader.dispose()
            self.closed = True  # only now are the files released
        logger.info("Closed keep %r", self.keep.config.name)

    def __enter__(self) -> "KeepSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
