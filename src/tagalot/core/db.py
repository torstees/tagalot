"""Engine and session setup, WAL, and schema versioning."""

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, Connection

from tagalot.core.keep import Keep

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5000
"""How long a connection waits for a lock before failing with "database is locked"."""


def create_keep_engine(db_path: Path, *, network: bool = False, read_only: bool = False) -> Engine:
    """Create an engine for a keep database file; the file is created if it does not exist.

    Every connection gets foreign keys on and a busy timeout. Local keeps use WAL, so readers
    never block the writer; keeps on a network share use the rollback journal instead, because
    WAL needs shared memory on one host and does not work over network file systems.

    ``read_only`` engines refuse writes (``PRAGMA query_only``), for UI query connections;
    only the DB writer writes (AGENTS.md rule 5).

    Transactions are issued by SQLAlchemy rather than Python's ``sqlite3`` module. Otherwise
    ``sqlite3`` commits before DDL, so rolling back a failed schema migration would leave
    tables half-created.
    """
    url = URL.create("sqlite+pysqlite", database=str(db_path))
    engine = create_engine(url)
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
