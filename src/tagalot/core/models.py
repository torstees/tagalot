"""Core ORM models: resources, entities, tags, links, and the closure table (DESIGN.md §5).

Theme tables are not defined here: they are built per keep as Core tables (§9).

Delete behavior: link rows (entity_resource, entity_contains, entity_ancestor, entity_tag,
field_provenance, tag_alias) cascade with the rows they link. Deleting a tag that has children
or a root that has resources is refused by the database; tag operations and root removal
handle those explicitly.
"""

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
    event,
    func,
)
from sqlalchemy.engine import Connection, Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator[datetime]):
    """Store datetimes as UTC and always return them timezone-aware.

    SQLite has no time zones; without this, values come back naive.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"naive datetime {value!r}; use an aware UTC datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    """The current time as an aware UTC datetime."""
    return datetime.now(UTC)


class ResourceKind(enum.StrEnum):
    FILE = "file"
    DIR = "dir"


class ResourceStatus(enum.StrEnum):
    OK = "ok"
    OFFLINE = "offline"
    """The root was unreachable at the last scan. Never a reason to delete (AGENTS.md rule 7)."""
    MISSING = "missing"
    """The root was reachable and the file was gone."""


class FieldSource(enum.StrEnum):
    EXTRACTED = "extracted"
    USER = "user"
    FETCHED = "fetched"


def _enum(cls: type[enum.StrEnum]) -> Enum:
    """Store an enum by value as text, with a CHECK constraint on the allowed values."""
    return Enum(
        cls,
        values_callable=lambda members: [m.value for m in members],
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=16,
    )


CASCADE = "CASCADE"


class Base(DeclarativeBase):
    """Declarative base for core tables, with stable constraint names for migrations."""

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    # A SQLAlchemy class-level setting, not per-instance state.
    type_annotation_map = {datetime: UTCDateTime(), dict[str, Any]: JSON()}  # noqa: RUF012


class Root(Base):
    """A watched directory; mirrors ``keep.toml`` roots for joins and status."""

    __tablename__ = "root"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    online: Mapped[bool] = mapped_column(default=False)
    last_scan_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    ingest_options: Mapped[str | None]
    """The theme options this root was last ingested with (JSON); when they change, the
    root's files are ingested again."""


class Resource(Base):
    """A file or directory under a root, identified by root id and relative POSIX path."""

    __tablename__ = "resource"
    __table_args__ = (Index(None, "root_id", "relpath", unique=True), Index(None, "fingerprint"))

    id: Mapped[int] = mapped_column(primary_key=True)
    root_id: Mapped[str] = mapped_column(ForeignKey("root.id"))
    relpath: Mapped[str]
    """POSIX-style path relative to the root, e.g. ``Artist/Album/01 Song.flac``."""
    kind: Mapped[ResourceKind] = mapped_column(_enum(ResourceKind))
    ext: Mapped[str] = mapped_column(String, default="")
    """Lowercase extension with the dot, or empty."""
    size: Mapped[int | None]
    mtime_ns: Mapped[int | None]
    """Modification time in integer nanoseconds (``os.stat().st_mtime_ns``) for exact diffing."""
    fingerprint: Mapped[bytes | None]
    ingested_at: Mapped[datetime | None]
    """When the theme last ingested this resource; ``None`` = pending (new, changed, or its
    ingest failed), so the next scan hands it to the theme again."""
    status: Mapped[ResourceStatus] = mapped_column(_enum(ResourceStatus), default=ResourceStatus.OK)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    parent_resource_id: Mapped[int | None] = mapped_column(
        ForeignKey("resource.id", ondelete=CASCADE)
    )
    """Reserved for archive members (DESIGN.md §14)."""


class Entity(Base):
    """Base row for every theme entity; each theme type's table shares its ``id``."""

    __tablename__ = "entity"
    __table_args__ = (Index(None, "type", "ingest_key", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str]
    """Namespaced type id such as ``music.song``."""
    title: Mapped[str]
    ingest_key: Mapped[str | None]
    """The theme's stable natural key; NULL keys never collide."""
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)
    """User-defined fields. Assign a new dict to change it; in-place edits are not tracked."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    thumb_resource_id: Mapped[int | None] = mapped_column(
        ForeignKey("resource.id", ondelete="SET NULL")
    )


class EntityResource(Base):
    """Links an entity to a resource in a role (``audio``, ``cover``, ``folder``…)."""

    __tablename__ = "entity_resource"
    __table_args__ = (Index(None, "resource_id"),)

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete=CASCADE), primary_key=True
    )
    role: Mapped[str] = mapped_column(primary_key=True)
    sort_order: Mapped[int] = mapped_column(default=0)


class EntityContains(Base):
    """A containment edge declared by a theme ingester (parent contains child)."""

    __tablename__ = "entity_contains"
    __table_args__ = (Index(None, "child_id"),)

    parent_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    child_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )


class EntityAncestor(Base):
    """Closure table: every (entity, ancestor) pair with the minimum depth; depth 0 is self."""

    __tablename__ = "entity_ancestor"
    __table_args__ = (Index(None, "ancestor_id", "entity_id"),)

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    ancestor_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    depth: Mapped[int]


class Tag(Base):
    """A node in the keep's tag tree."""

    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("tag.id"))
    name: Mapped[str]
    color: Mapped[str | None]
    sort_order: Mapped[int] = mapped_column(default=0)
    description: Mapped[str | None]  # shown in tooltips, matched when finding tags (core v2)


# Sibling names are unique case-insensitively, including at the root level: SQLite treats
# NULLs as distinct in unique indexes, so NULL parents are coalesced to 0 (DESIGN.md §5).
Index(
    "uq_tag_sibling_name",
    func.coalesce(Tag.parent_id, 0),
    func.lower(Tag.name),
    unique=True,
)


class TagAlias(Base):
    """An alternate name that matches a tag in the tag filter box."""

    __tablename__ = "tag_alias"

    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete=CASCADE), primary_key=True)
    alias: Mapped[str] = mapped_column(primary_key=True)


class EntityTag(Base):
    """A tag applied directly to an entity. Parent tags are never stored implicitly."""

    __tablename__ = "entity_tag"
    __table_args__ = (Index(None, "tag_id", "entity_id"),)

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete=CASCADE), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)


class FieldProvenance(Base):
    """Where a field's current value came from; ``user`` values are never overwritten."""

    __tablename__ = "field_provenance"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    field: Mapped[str] = mapped_column(primary_key=True)
    source: Mapped[FieldSource] = mapped_column(_enum(FieldSource))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SavedSearch(Base):
    """A named search; ``definition`` is the JSON form of a ``SearchSpec`` (§8)."""

    __tablename__ = "saved_search"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    definition: Mapped[dict[str, Any]]


class SchemaVersion(Base):
    """The schema version of the core (``component = "core"``) and of the keep's theme."""

    __tablename__ = "schema_version"

    component: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[int]


# --- Text search (DESIGN.md §5, §8) ---

FTS_TOKENIZER = "trigram remove_diacritics 1"
"""Substring matching, case-insensitive, ignoring diacritics ("bey" finds "Beyoncé")."""


@event.listens_for(Base.metadata, "after_create")
def _create_entity_fts(_target: MetaData, connection: Connection, **_kw: Any) -> None:
    connection.exec_driver_sql(
        "CREATE VIRTUAL TABLE IF NOT EXISTS entity_fts "
        f"USING fts5(title, body, tokenize = '{FTS_TOKENIZER}')"
    )


@event.listens_for(Base.metadata, "before_drop")
def _drop_entity_fts(_target: MetaData, connection: Connection, **_kw: Any) -> None:
    connection.exec_driver_sql("DROP TABLE IF EXISTS entity_fts")


entity_fts = Table(
    "entity_fts",
    MetaData(),  # not Base.metadata: SQLAlchemy can't create virtual tables; the DDL above does
    Column("rowid", Integer, primary_key=True),
    Column("title", Text),
    Column("body", Text),
)
"""Query handle for the FTS5 table: one row per entity, ``rowid = entity.id``.

``body`` holds the entity's ``search="text"`` field values and ``extra`` values. The DB
writer keeps it in sync; there are no triggers. Search with, e.g.,
``literal_column("entity_fts").match('"bey"')``.
"""
