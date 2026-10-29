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

from sqlalchemy import Connection, Engine, and_, func, or_, select, update
from sqlalchemy.exc import OperationalError

from tagalot.core.actions import ActionChange, ActionResult, delete_items, run_action
from tagalot.core.activity import (
    ProblemLog,
    contents_problems,
    online_problems,
    problems_from_report,
    thumbnail_problems,
)
from tagalot.core.color_schemes import all_schemes, resolve_colors, save_user_scheme
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
from tagalot.core.folder_items import tagged_hidden
from tagalot.core.fonts import copy_font_file, read_font_files, theme_font_files
from tagalot.core.google_fonts import GoogleFont, download_font, downloaded
from tagalot.core.icons import copy_icon_picture, keep_icon, theme_folder, type_icon
from tagalot.core.ingest import IngestSession
from tagalot.core.item_pictures import set_picture
from tagalot.core.keep import Keep, KeepConfig, allowed_hosts, open_keep, save_keep_config
from tagalot.core.keep_settings import (
    with_colors,
    with_contents_index,
    with_fonts,
    with_keep_icon,
    with_markdown_assets,
    with_name,
    with_note_ignore_keys,
    with_note_key_aliases,
    with_online_find_by,
    with_online_lookups,
    with_online_services,
    with_option,
    with_see_through,
    with_thumbnail_max,
    with_thumbnails_after_scan,
    with_type_icon,
)
from tagalot.core.keywords import any_keywords
from tagalot.core.links import link_files, unlink_file
from tagalot.core.merge import MergePlan, merge_items, plan_merge
from tagalot.core.models import Entity, EntityResource, Resource, ResourceStatus, Root
from tagalot.core.not_duplicates import Entry, set_not_duplicate
from tagalot.core.note_keys import (
    NoteKeyChange,
    check_alias,
    check_key,
    ignored_keys,
    key_aliases,
    with_alias,
    with_key,
)
from tagalot.core.notes_folder import notes_folder_files
from tagalot.core.online import (
    Fetcher,
    LookupReport,
    NotAllowedError,
    Opener,
    changed_since,
    look_up,
    to_fetch,
    urlopen_reply,
    usable,
)
from tagalot.core.online import Progress as OnlineProgress
from tagalot.core.own_fields import (
    OwnField,
    OwnFieldError,
    OwnFields,
    clash,
    field_problem,
    with_field,
    without_field,
)
from tagalot.core.own_values import count_values
from tagalot.core.picture_problems import (
    PictureProblem,
    PictureScope,
    ignore_pictures,
    stop_ignoring,
)
from tagalot.core.promote import (
    FieldMove,
    KeyValue,
    field_items,
    key_values,
    promote,
    promotion_items,
    recorded,
    turn_back,
)
from tagalot.core.reextract import ReextractReport, reextract
from tagalot.core.relations import add_related, move_related, remove_related
from tagalot.core.root_admin import (
    RemovalCounts,
    delete_root,
    edit_root,
    unwatch,
    with_notes_folder,
    with_notes_writing,
    without_root,
)
from tagalot.core.roots import place_in_roots, sync_roots
from tagalot.core.save_note import NewNote, plan_note, write_note
from tagalot.core.saved_searches import (
    SavedDefinition,
    delete_search,
    rename_search,
    save_search,
    set_search_icon,
)
from tagalot.core.scanjob import Progress, ScanReport, scan_notes_folder, scan_root
from tagalot.core.search import ContentsScope
from tagalot.core.search_spec import SearchSpec
from tagalot.core.see_through import names_of
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
from tagalot.themes.api import ColorScheme, OnlineSource, Theme, entity_fields
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
    _icons: dict[str, str | None] = field(default_factory=dict, init=False, repr=False)
    """Types' icon sources, found once (a picture's file is looked for) until ``keep.toml``
    changes."""
    colors: tuple[str, dict[str, str]] = field(default=("system", {}), init=False, repr=False)
    """The look ``keep.toml`` names (#572): ``(mode, colors)`` from
    :func:`~tagalot.core.color_schemes.resolve_colors`, found at open and when the
    configuration changes, so the window never reads a scheme's file."""
    font_files: dict[str, bytes | None] = field(default_factory=dict, init=False, repr=False)
    """The font files ``[appearance.fonts]`` names (#565), read at open and when the
    configuration changes: bytes by value, None for one that can't be read."""
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
            opened.schema,
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
        thumbnails.see_through = lambda: session.see_through
        thumbnails.icon_of = session.type_icon  # the keep's choices too (#557)
        thumbnails.report = lambda found: session.problems.add(thumbnail_problems(found))
        session.tags.on_note_key = session._list_note_key
        session.tags.own_fields = lambda: session.own_fields
        session.tags.on_own_fields = session._save_own_fields
        session.colors = resolve_colors(opened.keep.config, theme)
        session.font_files = session._read_font_files(opened.keep.config)
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
        theme, then match the notes folder's notes to items (§9 *The notes folder*). Runs
        in a worker."""
        reports = []
        self.last_scan_started = datetime.now(UTC)
        wanted = set(root_ids) if root_ids is not None else None
        config = self.keep.config
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
                    markdown_assets=config.markdown_assets,
                    progress=progress,
                    note_ignore_keys=self.note_ignore_keys,
                    note_key_aliases=self.note_key_aliases,
                    notes_folder=config.notes_path if root.id == config.notes_root else None,
                    see_through=self.see_through,
                )
            )
            self.problems.add(problems_from_report(reports[-1]))
        notes_root = next((r for r in config.roots if r.id == config.notes_root), None)
        if notes_root is not None or self._had_notes_folder():
            notes = scan_notes_folder(
                self.writer,
                self.reader,
                config.roots,
                self.root_path,
                notes_root.id if notes_root is not None else None,
                config.notes_path,
                theme=self.theme,
                schema=self.schema,
                theme_options=config.theme_options,
                markdown_assets=config.markdown_assets,
                progress=progress,
                see_through=self.see_through,
            )
            self.problems.add(problems_from_report(notes))
        return reports

    @property
    def see_through(self) -> frozenset[str]:
        """The keep's see-through folder names, case-folded (#459)."""
        return names_of(self.keep.config.see_through)

    def _had_notes_folder(self) -> bool:
        """Notes from a notes folder are recorded (to let go of when it is gone)."""
        with self.reader.connect() as conn:
            return bool(notes_folder_files(conn))

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
            markdown_assets=self.keep.config.markdown_assets,
            see_through=self.see_through,
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
        env = self.environment()
        fetcher = fetcher or Fetcher(
            self.allowed_online_sources(), opener=self.online_opener, stopped=stop, env=env
        )
        report = look_up(
            self.writer,
            self.reader,
            self.schema,
            theme,
            entity_ids,
            refresh=refresh,
            find_by=self.keep.config.online_find_by,
            env=env,
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
            return to_fetch(
                conn,
                self.schema,
                self.theme(),
                entity_ids,
                refresh=refresh,
                find_by=self.keep.config.online_find_by,
                env=self.environment(),
            )

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
        self._icons = {}  # looked up again (#557)
        self.colors = resolve_colors(config, self.theme)
        self.font_files = self._read_font_files(config)
        self.writer.run(lambda conn: sync_roots(conn, config.roots))

    # --- ignored note keys (core.note_keys, #381) ---

    @property
    def note_ignore_keys(self) -> frozenset[str]:
        """The front-matter keys notes leave out, case-folded: the built-in ones, the
        user's (``settings.toml``), and the keep's (``keep.toml``)."""
        return ignored_keys(self.settings.note_ignore_keys, self.keep.config.note_ignore_keys)

    @property
    def note_key_aliases(self) -> dict[str, str]:
        """The front-matter keys read as fields (#411), by case-folded key: the user's
        (``settings.toml``), then the keep's (``keep.toml``), which win."""
        return key_aliases(self.settings.note_key_aliases, self.keep.config.note_key_aliases)

    def set_note_key_aliases(self, aliases: Mapping[str, str]) -> None:
        """Set the keep's key aliases; they apply at the next scan, which reads the notes
        and Markdown files again. Runs in a worker."""
        self.save_config(with_note_key_aliases(self.keep.config, aliases))

    def alias_note_key(self, key: str, field_name: str) -> None:
        """Read ``key`` as the field ``field_name`` (its name or label) in this keep's notes
        and Markdown files (#411): listed in ``keep.toml``, it applies at the next scan.
        Runs in a worker."""
        key, field_name = check_alias(key, field_name)
        aliases = with_alias(self.keep.config.note_key_aliases, key, field_name)
        self.set_note_key_aliases(aliases)

    def set_markdown_assets(self, assets: str) -> None:
        """Set the keep's default assets folder (#418); it applies to pictures and links on
        items' pages at once, and to covers at the next scan. Runs in a worker."""
        self.save_config(with_markdown_assets(self.keep.config, assets))

    # --- the keep's own fields (#581, core.own_fields) ---

    @property
    def own_fields(self) -> OwnFields:
        """The keep's own fields, by type id and name (``keep.toml``'s ``[fields]``)."""
        return self.keep.config.fields

    def theme_fields(self, type_id: str) -> list[tuple[str, str]]:
        """A type's theme fields as ``(name, label)``; none for a type the theme hasn't."""
        try:
            entity = self.schema.by_type_id(type_id).entity
        except KeyError:
            return []
        return [(f.name, f.spec.label) for f in entity_fields(entity)]

    def own_field_problem(self, own: OwnField) -> str | None:
        """Why ``own`` can't be added (or changed to this), or None: a problem of its own,
        or a clash with its type's theme fields, title, or other own fields."""
        try:
            title_label = self.schema.by_type_id(own.type_id).entity.title_label
        except KeyError:
            return f"This keep's theme has no type {own.type_id!r}"
        others = self.own_fields.get(own.type_id, {}).values()
        return field_problem(own) or clash(own, self.theme_fields(own.type_id), title_label, others)

    def theme_clash(self, own: OwnField) -> str | None:
        """The theme field an own field is now named or labelled like (a later theme
        version added it), or None. Both keep their values; Keep configuration notes it."""
        names = {own.name.casefold(), own.label.casefold()}
        for name, label in self.theme_fields(own.type_id):
            if names & {name.casefold(), label.casefold()}:
                return label
        return None

    def add_own_field(self, own: OwnField) -> None:
        """Add one of the keep's own fields (#581). Raises :class:`OwnFieldError` for a
        field that can't be added. Runs in a worker."""
        if own.name in self.own_fields.get(own.type_id, {}):
            raise OwnFieldError(f"There is already a field named {own.name!r}")
        problem = self.own_field_problem(own)
        if problem is not None:
            raise OwnFieldError(problem)
        self.save_config(replace(self.keep.config, fields=with_field(self.own_fields, own)))

    def change_own_field(self, own: OwnField) -> None:
        """Change an own field's label, search, display, card, or column (its name and
        kind stay). Raises :class:`OwnFieldError`. Runs in a worker."""
        before = self.own_fields.get(own.type_id, {}).get(own.name)
        if before is None:
            raise OwnFieldError(f"There is no field named {own.name!r}")
        if before.kind != own.kind:
            raise OwnFieldError("A field's kind stays as made")
        problem = self.own_field_change_problem(own)
        if problem is not None:
            raise OwnFieldError(problem)
        self.save_config(replace(self.keep.config, fields=with_field(self.own_fields, own)))

    def own_field_change_problem(self, own: OwnField) -> str | None:
        """Why an own field can't be changed to ``own``, or None. A clash with a theme field
        a later theme version brought stays allowed while its label stays."""
        problem = field_problem(own)
        if problem is not None:
            return problem
        before = self.own_fields.get(own.type_id, {}).get(own.name)
        if before is not None and before.label == own.label and self.theme_clash(before):
            return None
        return self.own_field_problem(own)

    def count_own_values(self, own: OwnField) -> int:
        """How many items have a value of an own field (asked before removing it)."""
        with self.reader.connect() as conn:
            return count_values(conn, own)

    def remove_own_field(self, own: OwnField) -> FieldMove:
        """Turn an own field back into Extra fields (#582; Keep configuration's Remove):
        its values become text Extra fields named by its label, and it leaves
        ``keep.toml``. One undo step. Runs in a worker."""
        schema, before = self.schema, self.own_fields
        after = without_field(before, own.type_id, own.name)

        def work(conn: Connection) -> tuple[ActionChange, int]:
            ids = field_items(conn, own)
            return recorded(conn, schema, ids, lambda c: turn_back(c, schema, own))

        action, moved = self.writer.run(work)
        self._save_own_fields(after)
        items = "1 item" if moved == 1 else f"{moved} items"
        label = f"Turn {own.label!r} back into an Extra field ({items})"
        change = FieldMove(label, action, before, after, moved)
        self.tags.record(change)
        return change

    def extra_key_values(self, key: str) -> list[KeyValue]:
        """Every item's Extra field named ``key`` (case aside), for **Make it a
        field…**'s preview. Runs in a worker."""
        with self.reader.connect() as conn:
            return key_values(conn, key)

    def promote_extra(self, key: str, fields: Sequence[OwnField]) -> FieldMove:
        """Make the Extra field ``key`` a field of the keep's own (#582), one per type
        in ``fields``: values that read as its kind move into it with their provenance;
        the rest stay Extra fields. One undo step. Raises :class:`OwnFieldError` for a
        field that can't be added. Runs in a worker."""
        if not fields:
            raise OwnFieldError("Choose a type for the field")
        before = after = self.own_fields
        for own in fields:
            if own.name in after.get(own.type_id, {}):
                raise OwnFieldError(f"There is already a field named {own.name!r}")
            problem = self.own_field_problem(own)
            if problem is not None:
                raise OwnFieldError(problem)
            after = with_field(after, own)
        schema, types = self.schema, [f.type_id for f in fields]

        def work(conn: Connection) -> tuple[ActionChange, int]:
            ids = promotion_items(conn, key, types)
            return recorded(conn, schema, ids, lambda c: promote(c, schema, key, fields))

        action, moved = self.writer.run(work)
        self._save_own_fields(after)
        items = "1 item" if moved == 1 else f"{moved} items"
        label = f"Make {key!r} the field {fields[0].label!r} ({items})"
        change = FieldMove(label, action, before, after, moved)
        self.tags.record(change)
        return change

    def _save_own_fields(self, fields: OwnFields) -> None:
        self.save_config(replace(self.keep.config, fields={t: dict(f) for t, f in fields.items()}))

    # --- icons (#557, core.icons) ---

    def type_icon(self, type_id: str) -> str | None:
        """The source of a type's icon: the keep's choice, else the theme's; None for
        the generic one."""
        if type_id not in self._icons:
            try:
                entity = self.schema.by_type_id(type_id).entity
            except KeyError:
                return None
            self._icons[type_id] = type_icon(self.theme, entity, self.keep)
        return self._icons[type_id]

    def keep_icon(self) -> str | None:
        """The source of the keep's icon: its own, else its theme's."""
        return keep_icon(self.theme, self.keep)

    def set_type_icon(self, type_id: str, icon: str | None, picture: Path | None = None) -> None:
        """Choose a type's icon: a Lucide name, ``picture`` (copied into the keep folder),
        or None (with no picture) for the theme's. Raises ``KeepError`` or
        ``IconFileError``. Runs in a worker."""
        if picture is not None:
            icon = copy_icon_picture(self.keep.dir, picture)
        self.save_config(with_type_icon(self.keep.config, type_id, icon))

    def set_colors(self, mode: str, custom: Mapping[str, str] | None = None) -> None:
        """Choose the keep's colors: a mode (#558) or a color scheme's id (#572). Raises
        ``KeepError``. Runs in a worker."""
        self.save_config(with_colors(self.keep.config, mode, custom))

    def set_fonts(self, fonts: Mapping[str, str | None], text_size: int | None) -> None:
        """Choose the keep's fonts and text size (#565). Raises ``KeepError``. Runs in a
        worker."""
        self.save_config(with_fonts(self.keep.config, fonts, text_size))

    def font_file(self, source: Path) -> str:
        """Copy a font file into the keep folder for a font; its stored path. Raises
        ``FontFileError``. Runs in a worker."""
        return copy_font_file(self.keep.dir, source)

    def download_google_font(
        self, font: GoogleFont, progress: Callable[[int, int], None] | None = None
    ) -> str:
        """Download a Google Font into the keep folder (#566), only when the user asked;
        what ``keep.toml`` names it by. Raises ``GoogleFontError``. Runs in a worker and
        goes online."""
        value = download_font(self.keep.dir, font, progress=progress)
        self.font_files = self._read_font_files(self.keep.config)  # a theme's may be it
        return value

    def _read_font_files(self, config: KeepConfig) -> dict[str, bytes | None]:
        """The font files the keep's fonts name, and those its theme's suggestions may
        show (#567). Runs in a worker."""
        found = theme_font_files(
            self.keep.dir, getattr(self.theme, "fonts", {}), theme_folder(self.theme)
        )
        return {**found, **read_font_files(self.keep.dir, config.fonts)}

    def google_fonts_here(self) -> set[str]:
        """The Google Fonts already in the keep folder. Runs in a worker."""
        return downloaded(self.keep.dir)

    def color_schemes(self) -> list[ColorScheme]:
        """The schemes this keep can choose from: built in, its theme's, the user's."""
        return all_schemes(self.theme)

    def save_color_scheme(
        self, name: str, colors: Mapping[str, str], scheme_id: str | None = None
    ) -> ColorScheme:
        """Save one of the user's schemes (``scheme_id``: **Save**; None: **Save As**, a new
        one named ``name``), and give the keep its colors. Raises ``ColorSchemeError``
        and ``KeepError``. Runs in a worker."""
        scheme = save_user_scheme(name, colors, scheme_id=scheme_id)
        self.set_colors(scheme.id)
        return scheme

    def set_keep_icon(self, icon: str | None, picture: Path | None = None) -> None:
        """Choose the keep's icon, as :meth:`set_type_icon`. Runs in a worker."""
        if picture is not None:
            icon = copy_icon_picture(self.keep.dir, picture)
        self.save_config(with_keep_icon(self.keep.config, icon))

    def icon_picture(self, picture: Path) -> str:
        """Copy a picture into the keep folder for an icon; its stored path. Runs in a
        worker."""
        return copy_icon_picture(self.keep.dir, picture)

    def set_see_through(self, names: Iterable[str]) -> None:
        """Set the keep's see-through folder names (#459); they apply at the next scan,
        which reads every file again. Runs in a worker."""
        self.save_config(with_see_through(self.keep.config, names))

    def tagged_hidden_folders(self, names: Iterable[str]) -> int:
        """How many folder items the user tagged these see-through names would take away
        (#459). Runs in a worker."""
        with self.reader.connect() as conn:
            return tagged_hidden(conn, self.schema, names_of(names))

    def set_note_ignore_keys(self, keys: Iterable[str]) -> None:
        """Set the keep's ignored note keys; they apply at the next scan, which reads the
        notes again. Runs in a worker."""
        self.save_config(with_note_ignore_keys(self.keep.config, keys))

    def set_notes_folder(self, root_id: str | None, path: str = "") -> None:
        """Make ``path`` in the root ``root_id`` the keep's notes folder, or have none
        (``None``); it applies at the next scan. Runs in a worker."""
        self.save_config(with_notes_folder(self.keep.config, root_id, path))

    def set_notes_writing(
        self,
        writable: bool | None = None,
        new_notes: str | None = None,
        write_back: bool | None = None,
    ) -> None:
        """Let Save as note… write in the notes folder (or not), choose where it puts new
        notes (``"folders"`` or ``"one-folder"``), and let Write to file… change notes there
        (or not, #501). Runs in a worker."""
        self.save_config(with_notes_writing(self.keep.config, writable, new_notes, write_back))

    def save_as_note(self, entity_id: int) -> NewNote:
        """Write an item's typed note as a new file in the notes folder (Save as note…,
        #383); raises :class:`~tagalot.core.save_note.SaveNoteError` when it can't. The
        next scan of the notes folder reads it. Runs in a worker."""
        tree = self.tag_cache.get()
        with self.reader.connect() as conn:
            note = plan_note(conn, self.schema, tree, self.keep.config, self.root_path, entity_id)
        write_note(note)
        return note

    def ignore_note_key(self, key: str) -> NoteKeyChange:
        """Ignore ``key`` in every note of this keep: list it in ``keep.toml`` and remove
        the Extra fields notes gave under it now; one undo step. Runs in a worker."""
        key = check_key(key)
        listed = key.casefold() in {k.casefold() for k in self.keep.config.note_ignore_keys}
        if not listed:
            self._list_note_key(key, True)
        return self.tags.ignore_note_key(key, listed=listed)

    def _list_note_key(self, key: str, ignored: bool) -> None:
        keys = with_key(self.keep.config.note_ignore_keys, key, ignored)
        self.save_config(with_note_ignore_keys(self.keep.config, keys))

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

    def set_saved_icon(self, saved_id: int, icon: str, picture: Path | None = None) -> str:
        """Give a saved search the user's icon: a Lucide name, ``picture`` (copied into the
        keep folder), or the standard one (``""``); one undo step. Returns its label.
        Runs in a worker."""
        if picture is not None:
            icon = copy_icon_picture(self.keep.dir, picture)
        change = self.writer.run(lambda conn: set_search_icon(conn, saved_id, icon))
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

    def set_picture(self, entity_id: int, resource_id: int | None, label: str) -> str:
        """Make a file the item's own picture (``None``: use the usual picture again), as
        the user chose (#439); one undo step. Returns its label. Raises
        :class:`~tagalot.core.item_pictures.PictureError` for a file that can't be one.
        Runs in a worker."""
        change = self.writer.run(lambda conn: set_picture(conn, entity_id, resource_id, label))
        if change.before != change.after:
            self.tags.record(change)
        return change.label

    def picture_file(self, path: str) -> int | None:
        """The resource of the file at ``path`` (this computer's path), if a watched folder
        holds it and it was scanned. Runs in a worker."""
        roots = {r.id: self._known_root_path(r.id) for r in self.keep.config.roots}
        place = place_in_roots(path, roots)
        if place is None:
            return None
        root_id, relpath = place
        with self.reader.connect() as conn:
            found = select(Resource.id).where(
                Resource.root_id == root_id,
                func.lower(Resource.relpath) == relpath.lower(),
                Resource.status != ResourceStatus.MISSING,
            )
            return conn.scalar(found.limit(1))

    def ignore_pictures(
        self, problems: Sequence[PictureProblem], scope: PictureScope, label: str
    ) -> str:
        """Leave rows off Triage's Pictures list: for their items, their values everywhere,
        or their types (#426); one undo step. Returns its label, or ``""`` if nothing
        changed. Runs in a worker."""
        change = self.writer.run(lambda conn: ignore_pictures(conn, list(problems), scope, label))
        if change.added:
            self.tags.record(change)
        return change.label if change.added else ""

    def stop_ignoring_picture(self, problem: PictureProblem, label: str) -> str:
        """List an ignored Pictures row again, removing every rule that hides it; one undo
        step. Returns its label, or ``""``. Runs in a worker."""
        change = self.writer.run(lambda conn: stop_ignoring(conn, problem, label))
        if change.removed:
            self.tags.record(change)
        return change.label if change.removed else ""

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
        """Allow (``"allow"``, every service the theme uses) or refuse (``"never"``)
        online lookups for this keep, or forget the answer (``None``: ask again). Runs in a
        worker."""
        hosts = [s.host for s in self.online_sources()]
        self.save_config(with_online_lookups(self.keep.config, answer, hosts))

    def set_online_find_by(self, find_by: str) -> None:
        """Let items be identified to services by ``find_by`` (#485): ``"ids"``,
        ``"title-year"``, or ``"title"``. Runs in a worker."""
        self.save_config(with_online_find_by(self.keep.config, find_by))

    def online_sources(self) -> list[OnlineSource]:
        """The theme's services that can be used: those needing a token only while it is
        set (#485). The consent question names these."""
        return usable(self.theme.online_sources, self.environment())

    def environment(self) -> dict[str, str]:
        """This keep's environment variables for this user (#544, ``settings.toml``),
        used ahead of the real environment."""
        return self.settings.environment(self.keep.config.id)

    def allowed_online_sources(self) -> list[OnlineSource]:
        """The theme's services this keep may look things up in (#483)."""
        allowed = allowed_hosts(self.keep.config)
        return [s for s in self.online_sources() if s.host in allowed]

    def new_online_sources(self) -> list[OnlineSource]:
        """The theme's services a keep that allows lookups hasn't been asked about: added
        since the user allowed the others (#483), or whose token was set since (#485)."""
        config = self.keep.config
        if config.online_lookups != "allow":
            return []
        answered = allowed_hosts(config) | set(config.online_refused)
        return [s for s in self.online_sources() if s.host not in answered]

    def answer_online_sources(self, sources: Iterable[OnlineSource], allow: bool) -> None:
        """Allow, or don't use, these services, besides those already answered. Runs in a
        worker."""
        hosts = [s.host for s in sources]
        self.save_config(
            with_online_services(
                self.keep.config,
                allow=hosts if allow else (),
                refuse=() if allow else hosts,
                theme_hosts=[s.host for s in self.theme.online_sources],
            )
        )

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
