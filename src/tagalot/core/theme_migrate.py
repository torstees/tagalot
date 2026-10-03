"""Carrying out a theme's explicit schema changes (DESIGN.md §9 "Theme schema versions",
#173): renaming, retyping, and dropping fields, asked for in ``Theme.migrate_schema``.

These run in the upgrade's transaction, before the additive sync (``theme_db``) and
``Theme.migrate``. Themes never see SQL: :class:`CoreSchemaOps` checks each request against
the theme's declarations and the keep's actual tables, then does it with SQLite's own
``ALTER TABLE … RENAME COLUMN`` or, to retype or drop a column, a copy and swap (a new
table, the rows copied across, the old table dropped, the new one renamed, its indexes
made again). Each entity table's ``id`` keeps its foreign key to ``entity.id``.

Besides the table, a rename carries along field provenance (so user edits stay protected)
and saved searches' filters and sorts on that type; a drop removes the field's provenance.
"""

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    Column,
    Connection,
    Index,
    MetaData,
    Table,
    column,
    delete,
    insert,
    inspect,
    select,
    table,
    update,
)

from tagalot.core.keep import KeepError
from tagalot.core.models import FieldProvenance, SavedSearch
from tagalot.core.theme_schema import EntityTable, ThemeSchema, entity_key_column
from tagalot.themes.api import Entity as ThemeEntity

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
_DEFAULTS: dict[type, Any] = {str: "", int: 0, float: 0.0, bool: False}
"""What a value that can't be ``None`` becomes when it is missing (as the column defaults)."""
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


class SchemaMigrationError(KeepError):
    """A theme's schema change can't be made; the upgrade is rolled back. The message
    is for the user (and the theme's author)."""


class CoreSchemaOps:
    """The core's :class:`~tagalot.themes.api.SchemaOps`, for one upgrade's connection."""

    def __init__(self, conn: Connection, schema: ThemeSchema) -> None:
        self.conn = conn
        self.schema = schema
        self.changed: list[str] = []
        """What was done, e.g. ``tunes_song: tempo renamed to bpm``, for the log."""
        self.touched: set[str] = set()
        """Type ids whose text search should be refreshed once the schema is complete."""

    # --- SchemaOps ---

    def rename_field(self, entity: type[ThemeEntity], old: str, new: str) -> None:
        entity_table = self._entity_table(entity)
        where = f"{entity.__name__}: renaming {old!r} to {new!r}"
        self._declared(entity_table, new, where)
        self._not_declared(entity_table, old, where)
        present = self._columns(entity_table)
        if present is None or old not in present:
            return
        if new in present:
            raise SchemaMigrationError(f"{where}: the keep already has both fields.")
        name = entity_table.table.name
        self._drop_indexes(name, old)
        quote = self.conn.dialect.identifier_preparer.quote
        self.conn.exec_driver_sql(
            f"ALTER TABLE {quote(name)} RENAME COLUMN {quote(old)} TO {quote(new)}"
        )
        self.conn.execute(
            update(FieldProvenance)
            .where(FieldProvenance.field == old, FieldProvenance.entity_id.in_(self._ids(name)))
            .values(field=new)
        )
        renamed = self._rename_in_saved_searches(entity_table.type_id, old, new)
        self.changed.append(
            f"{name}: {old} renamed to {new}" + (f" ({renamed} saved searches)" if renamed else "")
        )

    def change_type(
        self,
        entity: type[ThemeEntity],
        name: str,
        convert: Callable[[Any], Any] | None = None,
    ) -> None:
        entity_table = self._entity_table(entity)
        where = f"{entity.__name__}.{name}: changing its type"
        info = self._declared(entity_table, name, where)
        present = self._columns(entity_table)
        if present is None or name not in present:
            return
        declared = entity_table.table.c[name]
        target, nullable = info.type, info.nullable

        def new_value(entity_id: int, raw: Any) -> Any:
            try:
                value = convert(raw) if convert is not None else convert_value(raw, target)
                return checked(value, target, nullable)
            except (ValueError, TypeError) as e:
                raise SchemaMigrationError(
                    f"{where}: item {entity_id}'s value {raw!r} can't be converted to "
                    f"{target.__name__} ({e})."
                ) from None

        replacement = Column(
            name, declared.type, nullable=nullable, server_default=declared.server_default
        )
        self._rebuild(entity_table, present, {name: (replacement, new_value)}, drop=None)
        self.touched.add(entity_table.type_id)
        self.changed.append(f"{entity_table.table.name}: {name} is now {target.__name__}")

    def drop_field(self, entity: type[ThemeEntity], name: str) -> None:
        entity_table = self._entity_table(entity)
        self._not_declared(entity_table, name, f"{entity.__name__}: dropping {name!r}")
        present = self._columns(entity_table)
        if present is None or name not in present:
            return
        table_name = entity_table.table.name
        self.conn.execute(
            delete(FieldProvenance).where(
                FieldProvenance.field == name, FieldProvenance.entity_id.in_(self._ids(table_name))
            )
        )
        self._rebuild(entity_table, present, {}, drop=name)
        self.touched.add(entity_table.type_id)
        self.changed.append(f"{table_name}: {name} dropped")

    # --- checks ---

    def _entity_table(self, entity: type[ThemeEntity]) -> EntityTable:
        found = self.schema.entities.get(entity)
        if found is None:
            name = getattr(entity, "__name__", repr(entity))
            raise SchemaMigrationError(f"{name} is not one of the theme's entities.")
        return found

    @staticmethod
    def _declared(entity_table: EntityTable, name: str, where: str) -> Any:
        for info in entity_table.fields:
            if info.name == name:
                return info
        raise SchemaMigrationError(f"{where}: {name!r} isn't a field the theme declares.")

    @staticmethod
    def _not_declared(entity_table: EntityTable, name: str, where: str) -> None:
        if any(info.name == name for info in entity_table.fields):
            raise SchemaMigrationError(f"{where}: the theme still declares {name!r}.")

    def _columns(self, entity_table: EntityTable) -> list[str] | None:
        """The table's columns in the keep, in order; ``None`` if it has no such table."""
        insp = inspect(self.conn)
        if not insp.has_table(entity_table.table.name):
            return None
        return [c["name"] for c in insp.get_columns(entity_table.table.name)]

    @staticmethod
    def _ids(table_name: str) -> Any:
        return select(column("id")).select_from(table(table_name))

    # --- doing it ---

    def _drop_indexes(self, table_name: str, name: str) -> None:
        """Drop the indexes on column ``name``; the additive sync makes the ones the theme
        declares again, under their new names."""
        quote = self.conn.dialect.identifier_preparer.quote
        for index in inspect(self.conn).get_indexes(table_name):
            if name in index["column_names"] and index["name"]:
                self.conn.exec_driver_sql(f"DROP INDEX {quote(index['name'])}")

    def _rebuild(
        self,
        entity_table: EntityTable,
        present: Sequence[str],
        replace: Mapping[str, tuple[Column[Any], Callable[[int, Any], Any]]],
        drop: str | None,
    ) -> None:
        """Copy and swap: the table again with ``replace``'s columns retyped (each value
        converted) and ``drop`` left out; other columns, rows, and indexes as they were."""
        name = entity_table.table.name
        old = Table(name, MetaData(), autoload_with=self.conn)
        indexes = [
            ix
            for ix in inspect(self.conn).get_indexes(name)
            if ix["name"] and drop not in ix["column_names"]
        ]
        kept = [c for c in present if c != drop]
        columns: list[Column[Any]] = []
        for c in kept:
            if c == "id":
                columns.append(entity_key_column("id"))
            elif c in replace:
                columns.append(replace[c][0])
            else:
                o = old.c[c]
                columns.append(
                    Column(c, o.type, nullable=o.nullable, server_default=o.server_default)
                )
        temp = f"{name}__tagalot_new"
        Table(temp, MetaData(), *columns).create(self.conn)
        # Values are copied as SQLite stores them (untyped columns), except converted ones,
        # which go through their new type's own storage conversion (a datetime to UTC text).
        dialect = self.conn.dialect
        storage = {
            c: col.type.dialect_impl(dialect).bind_processor(dialect)
            for c, (col, _) in replace.items()
        }
        target = table(temp, *(column(c) for c in kept))
        copied: Any = select(*(column(c) for c in kept)).select_from(table(name))
        rows: Sequence[Any] = self.conn.execute(copied).mappings().all()
        for batch in _batches(rows):
            values = []
            for row in batch:
                record = dict(row)
                for c, (_, convert) in replace.items():
                    value = convert(record["id"], record[c])
                    store = storage[c]
                    record[c] = store(value) if store is not None and value is not None else value
                values.append(record)
            self.conn.execute(insert(target), values)
        quote = self.conn.dialect.identifier_preparer.quote
        old.drop(self.conn)
        self.conn.exec_driver_sql(f"ALTER TABLE {quote(temp)} RENAME TO {quote(name)}")
        renamed = Table(name, MetaData(), autoload_with=self.conn)
        for ix in indexes:
            Index(ix["name"], *(renamed.c[c] for c in ix["column_names"] if c)).create(self.conn)

    def _rename_in_saved_searches(self, type_id: str, old: str, new: str) -> int:
        """Rename ``old`` in the filters and sorts of saved searches that can show items of
        ``type_id`` (searches of that type, or of every type). Returns how many changed."""
        count = 0
        saved = self.conn.execute(select(SavedSearch.id, SavedSearch.definition)).all()
        for search_id, definition in saved:
            if not isinstance(definition, dict):
                continue
            bare = "base" not in definition  # an older or hand-made row: just a search
            specs = [definition] if bare else [definition.get("base"), definition.get("filters")]
            base = specs[0] if isinstance(specs[0], dict) else {}
            types = base.get("types") or []
            if types and type_id not in types:
                continue
            changed = False
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                for key in ("fields", "sort"):
                    for item in spec.get(key) or []:
                        if isinstance(item, dict) and item.get("field") == old:
                            item["field"] = new
                            changed = True
            if changed:
                self.conn.execute(
                    update(SavedSearch)
                    .where(SavedSearch.id == search_id)
                    .values(definition=definition)
                )
                count += 1
        return count


def convert_value(raw: Any, target: type) -> Any:
    """``raw`` (as SQLite stores it) as a ``target`` value, if it clearly is one; blank text
    is ``None``. Raises ``ValueError`` otherwise. See ``SchemaOps.change_type``."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raise ValueError("binary data")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if target is str:
            return raw
        if target is int:
            try:
                return int(text)
            except ValueError:
                return _whole(float(text))
        if target is float:
            return float(text)
        if target is bool:
            word = text.lower()
            if word in _TRUE or word in _FALSE:
                return word in _TRUE
            raise ValueError("not yes or no")
        if target is date:
            try:
                return date.fromisoformat(text)
            except ValueError:
                return datetime.fromisoformat(text).date()
        if target is datetime:
            moment = datetime.fromisoformat(text)
            return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
    elif isinstance(raw, int | float):
        if target is str:
            return str(raw)
        if target is int:
            return _whole(raw)
        if target is float:
            return float(raw)
        if target is bool and raw in (0, 1):
            return bool(raw)
    raise ValueError("no clear conversion")


def checked(value: Any, target: type, nullable: bool) -> Any:
    """``value`` if it fits a field of type ``target``; a missing one becomes the default
    when the field can't be ``None``. Raises ``ValueError`` if it doesn't fit."""
    if value is None:
        if nullable:
            return None
        if target in _DEFAULTS:
            return _DEFAULTS[target]
        raise ValueError("it can't be empty")
    if target is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if target is datetime and isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if target is date and isinstance(value, datetime):
        raise ValueError("a date and time, not a date")
    if isinstance(value, target) and not (target is int and isinstance(value, bool)):
        return value
    raise ValueError(f"got a {type(value).__name__}")


def _whole(number: float) -> int:
    if isinstance(number, int) and not isinstance(number, bool):
        return number
    if float(number).is_integer():
        return int(number)
    raise ValueError("not a whole number")


def _batches(rows: Sequence[Any]) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch
