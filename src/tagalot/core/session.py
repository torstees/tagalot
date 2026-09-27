"""An open keep: everything the UI needs, opened and closed in the right order.

``KeepSession.open`` does file and database I/O (and may migrate), so the UI calls it from a
worker. It never imports Qt.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import Keep, open_keep
from tagalot.core.scanjob import Progress, ScanReport, scan_root
from tagalot.core.settings import Settings
from tagalot.core.tag_service import TagService
from tagalot.core.tags import TagTreeCache
from tagalot.core.theme_db import open_theme, resolve_theme
from tagalot.core.theme_schema import ThemeSchema, build_theme_schema
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
    closed: bool = field(default=False, init=False)

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
        cache = TagTreeCache(reader)
        logger.info("Opened keep %r with the %r theme", opened.keep.config.name, theme.id)
        return cls(
            keep=opened.keep,
            theme=theme,
            schema=opened.schema,
            writer=writer,
            reader=reader,
            tag_cache=cache,
            tags=TagService(writer, cache),
            settings=settings,
            catalog=catalog,
        )

    def root_path(self, root_id: str) -> str:
        """This machine's path for a root (per-user override, else ``keep.toml``)."""
        root = next(r for r in self.keep.config.roots if r.id == root_id)
        return self.settings.root_path(self.keep.config.id, root)

    def scan_all(self, progress: Progress | None = None) -> list[ScanReport]:
        """Scan every root in turn with the keep's theme. Runs in a worker."""
        reports = []
        for root in self.keep.config.roots:
            reports.append(
                scan_root(
                    self.writer,
                    self.reader,
                    root,
                    self.root_path(root.id),
                    theme=self.theme,
                    schema=self.schema,
                    progress=progress,
                )
            )
        return reports

    def close(self) -> None:
        """Finish queued writes and release the database. Safe to call twice."""
        if self.closed:
            return
        self.closed = True
        self.writer.close()
        self.reader.dispose()
        logger.info("Closed keep %r", self.keep.config.name)

    def __enter__(self) -> "KeepSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
