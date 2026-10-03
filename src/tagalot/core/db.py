"""Engine and session setup, WAL, and schema versioning."""

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, insert, inspect, select, text, update
from sqlalchemy.engine import URL, Connection
from sqlalchemy.pool import NullPool

from tagalot.core.keep import (
    KEEP_FORMAT_VERSION,
    Keep,
    KeepConfig,
    KeepError,
    save_keep_config,
)
from tagalot.core.models import Base, SchemaVersion

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5000
"""How long a connection waits for a lock before failing with "database is locked"."""

MIN_SQLITE_VERSION = (3, 45, 0)
"""Needed for the FTS5 trigram tokenizer's ``remove_diacritics`` option (DESIGN.md §3, §8)."""


def check_sqlite_support(
    version_info: tuple[int, ...] = sqlite3.sqlite_version_info,
    connect: Callable[[str], sqlite3.Connection] = sqlite3.connect,
) -> str | None:
    """Return a message explaining why this Python's SQLite can't run Tagalot, or ``None``.

    Checks the version, then creates the kind of FTS5 table text search uses, since FTS5 can
    be compiled out of an otherwise recent SQLite.
    """
    found = ".".join(str(n) for n in version_info)
    needed = ".".join(str(n) for n in MIN_SQLITE_VERSION)
    advice = (
        "Tagalot uses the SQLite library built into Python. Install a Python whose SQLite "
        "includes it, for example with `uv python install 3.12`."
    )
    if tuple(version_info) < MIN_SQLITE_VERSION:
        return f"Tagalot needs SQLite {needed} or newer, but this Python has {found}. {advice}"
    try:
        conn = connect(":memory:")
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE probe USING fts5("
                "title, tokenize = 'trigram remove_diacritics 1')"
            )
        finally:
            conn.close()
    except sqlite3.Error as e:
        return (
            f"Tagalot needs SQLite's FTS5 full-text search with the trigram tokenizer, which "
            f"this Python's SQLite {found} does not provide ({e}). {advice}"
        )
    return None


def create_keep_engine(db_path: Path, *, network: bool = False, read_only: bool = False) -> Engine:
    """Create an engine for a keep database file; the file is created if it does not exist.

    Every connection gets foreign keys on and a busy timeout. Local keeps use WAL, so readers
    never block the writer; keeps on a network share use the rollback journal instead, because
    WAL needs shared memory on one host and does not work over network file systems.

    ``read_only`` engines refuse writes (``PRAGMA query_only``), for UI query connections;
    only the DB writer writes (AGENTS.md rule 5). They don't pool connections: each one closes
    when its query is done. Otherwise a query still running in a worker when the keep closes
    would return its connection to the discarded pool, and the file would stay open (on
    Windows, locked) until that pool was garbage-collected.

    Transactions are issued by SQLAlchemy rather than Python's ``sqlite3`` module. Otherwise
    ``sqlite3`` commits before DDL, so rolling back a failed schema migration would leave
    tables half-created.
    """
    url = URL.create("sqlite+pysqlite", database=str(db_path))
    engine = create_engine(url, poolclass=NullPool) if read_only else create_engine(url)
    journal_mode = "delete" if network else "wal"

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        # Let SQLAlchemy emit BEGIN itself so DDL stays inside transactions (see docstring).
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            if not read_only:
                (mode,) = cursor.execute(f"PRAGMA journal_mode = {journal_mode}").fetchone()
                if mode != journal_mode:
                    logger.warning("%s: journal_mode is %s, wanted %s", db_path, mode, journal_mode)
            # NORMAL is safe with WAL (a crash can lose only the last commits, never corrupt);
            # the rollback journal keeps SQLite's default FULL.
            cursor.execute(f"PRAGMA synchronous = {'FULL' if network else 'NORMAL'}")
            if read_only:
                cursor.execute("PRAGMA query_only = ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN")

    return engine


def open_keep_engine(keep: Keep, *, read_only: bool = False) -> Engine:
    """Create an engine for ``keep``'s database, choosing the journal mode by its location."""
    return create_keep_engine(keep.db_path, network=keep.on_network, read_only=read_only)


CORE = "core"
"""The ``schema_version.component`` of the core schema (themes use their theme id)."""

Migration = Callable[[Connection], None]
"""Upgrades the core schema from version N to N + 1 inside the caller's transaction."""


def _v1_tag_descriptions(conn: Connection) -> None:
    """Format 2: tags get an optional description (#192)."""
    conn.execute(text("ALTER TABLE tag ADD COLUMN description VARCHAR"))


def _v2_root_ingest_options(conn: Connection) -> None:
    """Format 3: roots remember the theme options they were ingested with (#82)."""
    conn.execute(text("ALTER TABLE root ADD COLUMN ingest_options VARCHAR"))


def _v3_triage_dismissals(conn: Connection) -> None:
    """Format 4: what the user dismissed from triage lists (#110)."""
    Base.metadata.tables["triage_dismissal"].create(conn)


def _v4_skipped_resources(conn: Connection) -> None:
    """Format 5: files scans now leave out are flagged skipped, not missing (#152)."""
    conn.execute(text("ALTER TABLE resource ADD COLUMN skipped BOOLEAN NOT NULL DEFAULT 0"))


def _v5_user_links(conn: Connection) -> None:
    """Format 6: links made by hand are marked, so scans respect them (#243)."""
    conn.execute(text("ALTER TABLE entity_resource ADD COLUMN by_user BOOLEAN NOT NULL DEFAULT 0"))


def _v6_entity_merges(conn: Connection) -> None:
    """Format 7: items merged into others are remembered (#120)."""
    Base.metadata.tables["entity_merge"].create(conn)
    Base.metadata.tables["entity_merge_resource"].create(conn)


def _v7_dedupe_dismissals(conn: Connection) -> None:
    """Format 8: groups and pairs marked as not duplicates (#121)."""
    Base.metadata.tables["dedupe_dismissal"].create(conn)


CORE_MIGRATIONS: Mapping[int, Migration] = {
    1: _v1_tag_descriptions,
    2: _v2_root_ingest_options,
    3: _v3_triage_dismissals,
    4: _v4_skipped_resources,
    5: _v5_user_links,
    6: _v6_entity_merges,
    7: _v7_dedupe_dismissals,
}
"""Core migration steps keyed by the version they upgrade *from*."""


class KeepVersionError(KeepError):
    """The keep was written by a newer Tagalot, or its version information is damaged."""


class KeepNeedsMigration(KeepError):
    """The keep's core schema (or its theme's) is older than this Tagalot or theme; the user
    should confirm migrating.

    Reopen with ``allow_migration=True`` to back up ``keep.db`` and upgrade it.
    """

    def __init__(self, message: str, stored: int, current: int, component: str = "core") -> None:
        super().__init__(message)
        self.stored = stored
        self.current = current
        self.component = component
        """``"core"`` or the theme id."""


def open_keep_database(
    keep: Keep,
    *,
    allow_migration: bool = False,
    current_version: int = KEEP_FORMAT_VERSION,
    migrations: Mapping[int, Migration] = CORE_MIGRATIONS,
) -> tuple[Keep, Engine]:
    """Open ``keep``'s database, creating or upgrading the core schema as needed.

    Returns the keep (with ``format_version`` updated after a migration) and a writer engine.

    - New database: the core tables and the ``schema_version`` row are created.
    - Same version: opened as is.
    - Newer (in ``keep.toml`` or the database): :class:`KeepVersionError`; nothing is touched.
    - Older: :class:`KeepNeedsMigration` unless ``allow_migration``; then ``keep.db`` is
      backed up next to itself, migrated in one transaction, and ``keep.toml`` is updated.
    """
    newer = "It was created by a newer version of Tagalot"
    if keep.config.format_version > current_version:
        raise KeepVersionError(
            f"{newer} (keep format {keep.config.format_version}; this version supports "
            f"{current_version}). Update Tagalot to open it."
        )
    existed = keep.db_path.exists()
    engine = open_keep_engine(keep)
    try:
        stored = _stored_core_version(engine) if existed else None
        if stored is None:
            _create_core_schema(engine, current_version)
            logger.info("Created core schema v%d in %s", current_version, keep.db_path)
        elif stored > current_version:
            raise KeepVersionError(
                f"{newer} (database schema {stored}; this version supports {current_version}). "
                "Update Tagalot to open it."
            )
        elif stored < current_version:
            if not allow_migration:
                raise KeepNeedsMigration(
                    f"This keep uses an older database format ({stored}) and must be upgraded "
                    f"to format {current_version}. A backup of keep.db is made first.",
                    stored,
                    current_version,
                )
            backup = backup_database(engine, keep.db_path, f"v{stored}")
            logger.info("Backed up %s to %s before migrating", keep.db_path, backup)
            _migrate(engine, stored, current_version, migrations)
            logger.info("Migrated core schema v%d -> v%d", stored, current_version)
    except BaseException:
        engine.dispose()
        raise
    if keep.config.format_version != current_version:
        config: KeepConfig = replace(keep.config, format_version=current_version)
        save_keep_config(config, keep.toml_path)
        keep = replace(keep, config=config)
    return keep, engine


def backup_database(engine: Engine, db_path: Path, label: str) -> Path:
    """Copy the database with SQLite's backup API (safe with WAL) to a timestamped file."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = db_path.with_name(f"{db_path.name}.{label}-{stamp}.bak")
    raw = engine.raw_connection()
    try:
        dest = sqlite3.connect(target)
        try:
            raw.driver_connection.backup(dest)  # type: ignore[union-attr]
        finally:
            dest.close()
    finally:
        raw.close()
    return target


def _stored_core_version(engine: Engine) -> int | None:
    """The stored core version, or None for an empty database."""
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        if not tables:
            return None
        if "schema_version" not in tables:
            raise KeepVersionError(
                "keep.db has tables but no schema version; it may be damaged or not a keep "
                "database."
            )
        version = conn.scalar(select(SchemaVersion.version).where(SchemaVersion.component == CORE))
    if version is None:
        raise KeepVersionError("keep.db has no core schema version; it may be damaged.")
    return version


def _create_core_schema(engine: Engine, version: int) -> None:
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        conn.execute(insert(SchemaVersion).values(component=CORE, version=version))


def _migrate(
    engine: Engine, stored: int, current: int, migrations: Mapping[int, Migration]
) -> None:
    missing = [v for v in range(stored, current) if v not in migrations]
    if missing:
        raise KeepVersionError(
            f"No upgrade path from database format {stored} to {current} "
            f"(missing step from {missing[0]})."
        )
    with engine.begin() as conn:  # all steps succeed or none do
        for version in range(stored, current):
            migrations[version](conn)
        conn.execute(
            update(SchemaVersion).where(SchemaVersion.component == CORE).values(version=current)
        )
