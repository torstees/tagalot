"""Turning a theme's declarations into tables (DESIGN.md §5 "Theme tables", §9).

Built once per keep open, into a fresh ``MetaData``: a broken theme affects only keeps that
use it, and building the same theme again (a reload) is always safe. Theme tables are Core
``Table`` objects, not ORM classes; each entity table's ``id`` references ``entity.id``.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.types import TypeEngine

from tagalot.core.models import Base, Entity, UTCDateTime
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import (
    FieldInfo,
    Relationship,
    Theme,
    ThemeDeclarationError,
    entity_fields,
)

_COLUMN_TYPES: dict[type, TypeEngine[Any]] = {
    str: Text(),
    int: Integer(),
    float: Float(),
    bool: Boolean(),
    date: Date(),
    datetime: UTCDateTime(),
}
_DEFAULTS: dict[type, Any] = {str: text("''"), int: text("0"), float: text("0.0"), bool: false()}
"""Database defaults for non-nullable fields, so ingest may omit them."""
_INDEXED_SEARCH = {"range", "choice"}
_RESERVED_FIELDS = {"id"}


class SchemaBuildError(ValueError):
    """A theme's declarations can't be turned into tables; lists every problem found."""

    def __init__(self, theme: type[Theme], problems: list[str]) -> None:
        name = getattr(theme, "id", theme.__name__)
        super().__init__(f"Theme {name!r} has problems:\n- " + "\n- ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class EntityTable:
    entity: type[ThemeEntity]
    type_id: str
    table: Table
    fields: tuple[FieldInfo, ...]


@dataclass(frozen=True)
class RelationshipTable:
    relationship: Relationship
    table: Table


@dataclass(frozen=True)
class ThemeSchema:
    """A theme's tables for one open keep."""

    theme: type[Theme]
    metadata: MetaData
    entities: dict[type[ThemeEntity], EntityTable]
    relationships: dict[str, RelationshipTable]

    def by_type_id(self, type_id: str) -> EntityTable:
        for entity_table in self.entities.values():
            if entity_table.type_id == type_id:
                return entity_table
        raise KeyError(type_id)


def entity_key_column(name: str) -> Column[int]:
    """A primary-key column referencing ``entity.id``; deleting the entity deletes the row."""
    return Column(
        name, Integer, ForeignKey(Entity.__table__.c.id, ondelete="CASCADE"), primary_key=True
    )


def build_theme_schema(theme: type[Theme]) -> ThemeSchema:
    """Build ``theme``'s tables in a new ``MetaData``. Raises :class:`SchemaBuildError`
    listing every problem, so a theme author sees them all at once."""
    problems: list[str] = []
    metadata = MetaData()
    theme_id = getattr(theme, "id", "")
    if not isinstance(theme_id, str) or not theme_id.isidentifier() or theme_id != theme_id.lower():
        raise SchemaBuildError(theme, [f"theme id {theme_id!r} must be a lowercase identifier"])
    core_tables = set(Base.metadata.tables) | {"entity_fts"}

    entities: dict[type[ThemeEntity], EntityTable] = {}
    seen_tables: dict[str, str] = {}
    seen_types: dict[str, str] = {}
    for entity in theme.entities:
        where = entity.__name__
        table_name, type_id = theme.table_name_of(entity), theme.type_id_of(entity)
        if not table_name.startswith(f"{theme_id}_"):
            problems.append(f"{where}: table name {table_name!r} must start with '{theme_id}_'")
        if not type_id.startswith(f"{theme_id}."):
            problems.append(f"{where}: type id {type_id!r} must start with '{theme_id}.'")
        for attribute in ("label", "plural"):
            value = getattr(entity, attribute, None)
            if value is not None and not (isinstance(value, str) and value.strip()):
                problems.append(f"{where}: {attribute} must be a non-empty string or None")
        # A conflicting table name is reported, and the entity's fields are still checked,
        # but its table is not created (the name is taken).
        name_taken = table_name in core_tables or table_name in seen_tables
        if table_name in core_tables:
            problems.append(f"{where}: table name {table_name!r} is used by the core")
        for seen, name, what in (
            (seen_tables, table_name, "table"),
            (seen_types, type_id, "type id"),
        ):
            if name in seen:
                problems.append(f"{where}: {what} {name!r} is also used by {seen[name]}")
            else:
                seen[name] = where
        try:
            fields = entity_fields(entity)
        except ThemeDeclarationError as e:
            problems.append(str(e))
            continue
        columns: list[Column[Any]] = [entity_key_column("id")]
        indexes = []
        for f in fields:
            if f.name in _RESERVED_FIELDS or f.name.startswith("_"):
                problems.append(f"{where}.{f.name}: that field name is reserved")
                continue
            if not f.nullable and f.type in (date, datetime):
                problems.append(
                    f"{where}.{f.name}: date and time fields must allow None "
                    f"(write `{f.type.__name__} | None`), since there is no sensible default"
                )
                continue
            columns.append(
                Column(
                    f.name,
                    _COLUMN_TYPES[f.type],
                    nullable=f.nullable,
                    server_default=None if f.nullable else _DEFAULTS[f.type],
                )
            )
            if f.spec.search in _INDEXED_SEARCH:
                indexes.append(Index(f"ix_{table_name}_{f.name}", f.name))
        if name_taken:
            continue
        table = Table(table_name, metadata, *columns, *indexes)
        entities[entity] = EntityTable(entity, type_id, table, tuple(fields))

    relationships: dict[str, RelationshipTable] = {}
    for rel in theme.relationships:
        where = f"relationship {rel.name!r}"
        missing = [e.__name__ for e in (rel.a, rel.b) if e not in theme.entities]
        if missing:
            problems.append(f"{where}: {', '.join(missing)} is not one of the theme's entities")
            continue
        if rel.name in relationships:
            problems.append(f"{where} is declared twice")
            continue
        table_name = f"{theme_id}_{rel.name}"
        if table_name in seen_tables or table_name in core_tables:
            problems.append(f"{where}: table name {table_name!r} is already used")
            continue
        seen_tables[table_name] = where
        constraints: list[Any] = [Index(f"ix_{table_name}_b_id", "b_id")]
        if not rel.many:
            constraints.append(UniqueConstraint("b_id", name=f"uq_{table_name}_b_id"))
        ordered = [Column("position", Integer, nullable=True)] if rel.ordered else []
        table = Table(
            table_name,
            metadata,
            entity_key_column("a_id"),
            entity_key_column("b_id"),
            *ordered,  # each b's a items in order (#317)
            *constraints,
        )
        relationships[rel.name] = RelationshipTable(rel, table)

    if problems:
        raise SchemaBuildError(theme, problems)
    return ThemeSchema(theme, metadata, entities, relationships)
