"""Creating, checking, and migrating a keep's theme tables (DESIGN.md §9 "Theme schema
versions").

The theme version is kept in ``keep.toml`` (``[theme] version``) and in ``schema_version``
(``component`` = theme id), mirroring the core version (§5). On every open the core also
applies *additive* changes itself: missing tables, columns, and indexes are created, so a
theme that only adds entity types or fields needs no migration code. Removed fields are left
in place; changing a field's type is not supported.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from sqlalchemy import Connection, Engine, Table, insert, inspect, select, update
from sqlalchemy.schema import CreateColumn

from tagalot.core.db import KeepNeedsMigration, KeepVersionError, backup_database
from tagalot.core.keep import Keep, KeepError, save_keep_config
from tagalot.core.models import SchemaVersion
from tagalot.core.theme_schema import SchemaBuildError, ThemeSchema, build_theme_schema
from tagalot.themes.api import IngestContext, Theme

logger = logging.getLogger(__name__)

ContextFactory = Callable[[Connection], IngestContext]
"""Builds the ``ctx`` passed to ``Theme.migrate`` for a connection (the core's, from #43)."""


class KeepThemeError(KeepError):
    """The keep's theme isn't available or can't be used; the message says why."""


@dataclass
class SchemaChanges:
    """What the additive sync created, for logging and the activity panel."""

    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    """``table.column``."""
    indexes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.tables or self.columns or self.indexes)


@dataclass(frozen=True)
class OpenTheme:
    keep: Keep
    """The keep, with ``keep.toml``'s theme version updated if it changed."""
    schema: ThemeSchema
    changes: SchemaChanges
    migrated_from: int | None
    """The theme version migrated from, if a migration ran."""


def open_theme(
    engine: Engine,
    keep: Keep,
    theme: type[Theme],
    *,
    allow_migration: bool = False,
    make_context: ContextFactory | None = None,
) -> OpenTheme:
    """Make ``keep``'s database ready for ``theme``. Run after the core schema is open.

    - New keep: create the theme's tables and record its version.
    - Same version: open (applying any additive changes).
    - Newer in ``keep.toml`` or the database: :class:`KeepVersionError`; nothing changes.
    - Older: :class:`KeepNeedsMigration` unless ``allow_migration``; then back up
      ``keep.db`` and, in one transaction, apply additive changes, run
      ``theme().migrate(stored, ctx)``, and record the new version.
    """
    wanted = keep.config.theme
    if theme.id != wanted.id:
        raise KeepThemeError(f"This keep uses the {wanted.id!r} theme, not {theme.id!r}.")
    newer = f"The {theme.name!r} theme data was created by a newer version of that theme"
    if wanted.version > theme.version:
        raise KeepVersionError(
            f"{newer} (keep.toml says {wanted.version}; installed: {theme.version}). "
            "Update the theme to open this keep."
        )
    try:
        schema = build_theme_schema(theme)
    except SchemaBuildError as e:
        raise KeepThemeError(str(e)) from e

    with engine.connect() as conn:
        stored = conn.scalar(
            select(SchemaVersion.version).where(SchemaVersion.component == theme.id)
        )
    migrated_from: int | None = None
    if stored is not None and stored > theme.version:
        raise KeepVersionError(
            f"{newer} (database has {stored}; installed: {theme.version}). "
            "Update the theme to open this keep."
        )
    if stored is not None and stored < theme.version:
        if not allow_migration:
            raise KeepNeedsMigration(
                f"This keep's {theme.name!r} data is from theme version {stored} and must be "
                f"upgraded to version {theme.version}. A backup of keep.db is made first.",
                stored,
                theme.version,
                component=theme.id,
            )
        backup = backup_database(engine, keep.db_path, f"{theme.id}-v{stored}")
        logger.info("Backed up %s to %s before migrating the theme", keep.db_path, backup)
        migrated_from = stored

    with engine.begin() as conn:
        changes = sync_theme_schema(conn, schema)
        if migrated_from is not None:
            if make_context is None:
                raise KeepThemeError("Can't migrate the theme: no ingest context available.")
            theme().migrate(migrated_from, make_context(conn))
            conn.execute(
                update(SchemaVersion)
                .where(SchemaVersion.component == theme.id)
                .values(version=theme.version)
            )
        elif stored is None:
            conn.execute(insert(SchemaVersion).values(component=theme.id, version=theme.version))
    if changes:
        logger.info("Updated %r theme tables: %s", theme.id, changes)

    if wanted.version != theme.version:
        config = replace(keep.config, theme=replace(wanted, version=theme.version))
        save_keep_config(config, keep.toml_path)
        keep = replace(keep, config=config)
    return OpenTheme(keep, schema, changes, migrated_from)


def sync_theme_schema(conn: Connection, schema: ThemeSchema) -> SchemaChanges:
    """Create missing theme tables, columns, and indexes (additive changes only)."""
    changes = SchemaChanges()
    insp = inspect(conn)
    existing_tables = set(insp.get_table_names())
    for table in schema.metadata.sorted_tables:
        if table.name not in existing_tables:
            table.create(conn)
            changes.tables.append(table.name)
            continue
        present = {c["name"] for c in insp.get_columns(table.name)}
        for column in table.columns:
            if column.name not in present:
                _add_column(conn, table, column.name)
                changes.columns.append(f"{table.name}.{column.name}")
        indexed = {ix["name"] for ix in insp.get_indexes(table.name)}
        for index in table.indexes:
            if index.name not in indexed:
                index.create(conn)
                changes.indexes.append(str(index.name))
    return changes


def _add_column(conn: Connection, table: Table, name: str) -> None:
    """``ALTER TABLE … ADD COLUMN``, which SQLAlchemy Core has no construct for.

    The DDL comes from SQLAlchemy's own column compiler; table and column names are theme
    identifiers already validated by the schema builder. New non-nullable columns always
    carry a database default (§5), which SQLite requires for ``ADD COLUMN … NOT NULL``.
    """
    column_ddl = CreateColumn(table.c[name]).compile(dialect=conn.dialect)
    table_name = conn.dialect.identifier_preparer.quote(table.name)
    conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_ddl}")
