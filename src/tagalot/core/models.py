"""Core ORM models: resources, entities, tags, links, and the closure table (DESIGN.md §5).

Theme tables are not defined here: they are built per keep as Core tables (§9).

Delete behavior: link rows (entity_resource, entity_contains, entity_ancestor, entity_tag,
field_provenance, tag_alias) cascade with the rows they link. Deleting a tag that has children
or a root that has resources is refused by the database; tag operations and root removal
handle those explicitly.
"""

import enum
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
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
    text,
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
    NOTE = "note"
    """From the item's note (§9 *Notes*, core format 15): below the files' own details,
    above online details."""


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
    __table_args__ = (
        Index(None, "root_id", "relpath", unique=True),
        Index(None, "fingerprint"),
        # The few skipped files, for hiding items whose files are all skipped (#526).
        Index("ix_resource_skipped", "id", sqlite_where=text("skipped = 1")),
    )

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
    skipped: Mapped[bool] = mapped_column(default=False, server_default="0")
    """Left out of scans now (an exclude pattern or an extension the theme no longer takes);
    its status is as last seen. Not missing: the file may well be there (#152)."""
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
    by_user: Mapped[bool] = mapped_column(default=False, server_default="0")
    """Made by hand (#243). Themes don't see it from the file's side (``entities_of``),
    can't unlink it, and can't displace it in a one-file role; it counts as the item's
    file everywhere else."""
    by_folder: Mapped[bool] = mapped_column(default=False, server_default="0")
    """Made by the core from a gallery role's ``folders`` (#387, ``core.gallery_fill``):
    scans add and remove these as pictures come and go. Themes don't see it from the
    file's side; a theme linking the same file in the role makes the link its own."""


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
    types: Mapped[str | None]
    """The item types this tag (and its sub-tags) may be applied to, as space-separated type
    ids (``"books.book books.comic"``); ``None``: every type (core format 10, #135)."""
    icon: Mapped[str | None]
    """An emoji or a single character shown as a badge on items with the tag (or a sub-tag
    without an icon of its own); ``None``: no icon (core format 18, #398)."""


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


class TagGroup(Base):
    """A named group of tags (#534, DESIGN.md §7 *Tag groups*), so the Tags panel can show
    only the tags a kind of tagging needs. Names are unique, ignoring case (core format
    29)."""

    __tablename__ = "tag_group"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    sort_order: Mapped[int] = mapped_column(default=0)


Index("uq_tag_group_name", func.lower(TagGroup.name), unique=True)


class TagGroupMember(Base):
    """A tag in a group (#534); its sub-tags come along with it, never stored."""

    __tablename__ = "tag_group_member"
    __table_args__ = (Index(None, "tag_id"),)

    group_id: Mapped[int] = mapped_column(
        ForeignKey("tag_group.id", ondelete=CASCADE), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete=CASCADE), primary_key=True)


class EntityTag(Base):
    """A tag applied directly to an entity. Parent tags are never stored implicitly."""

    __tablename__ = "entity_tag"
    __table_args__ = (Index(None, "tag_id", "entity_id"),)

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete=CASCADE), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    by_file: Mapped[bool] = mapped_column(default=False, server_default="0")
    """There only because one of the item's files has a keyword matching the tag (core format
    11, #294, DESIGN.md §7 "File keywords"); scans keep these in step with the files."""


class EntityKeyword(Base):
    """A keyword one of an item's files gives it: front matter's tags, an EPUB subject
    (#294). It becomes a tag only by matching one the user defined."""

    __tablename__ = "entity_keyword"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete=CASCADE), primary_key=True
    )
    match_key: Mapped[str] = mapped_column(primary_key=True, index=True)
    """The keyword as it is matched: case and accents ignored, a path's levels joined by
    ``/`` (``core.keywords.keyword_key``)."""
    keyword: Mapped[str]
    """As the file wrote it."""


class KeywordIgnored(Base):
    """A keyword the user chose to ignore: not listed as unmatched (#295)."""

    __tablename__ = "keyword_ignored"

    match_key: Mapped[str] = mapped_column(primary_key=True)


class FileTagRemoval(Base):
    """A tag the user took off an item although its file's keyword matches it: scans leave it
    off (#294). Adding the tag back by hand, or Restore, deletes this."""

    __tablename__ = "file_tag_removal"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete=CASCADE), primary_key=True)


class FieldProvenance(Base):
    """Where a field's current value came from; ``user`` values are never overwritten."""

    __tablename__ = "field_provenance"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    field: Mapped[str] = mapped_column(primary_key=True)
    source: Mapped[FieldSource] = mapped_column(_enum(FieldSource))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class TriageDismissal(Base):
    """Something the user dismissed from a triage list (§12): hidden until it changes.

    ``target_id`` is a resource id (``unlinked``) or an entity id (``untagged``). ``marker``
    is what it looked like when dismissed (a file's size and time, an item's
    ``updated_at``); once that differs, it shows again. Rows of things since deleted are
    harmless and ignored.
    """

    __tablename__ = "triage_dismissal"

    list: Mapped[str] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(primary_key=True)
    marker: Mapped[str]
    dismissed_at: Mapped[datetime] = mapped_column(default=utcnow)


class EntityMerge(Base):
    """An item merged into another (§13, #120): it no longer exists, and this remembers it.

    When a theme's ``upsert`` asks for ``(type, ingest_key)`` again, or ``entities_of``
    for one of its files (:class:`EntityMergeResource`), the ingest session answers with the
    merged item and drops what the theme writes to it: the item stays merged. Pages and
    saved searches that name ``merged_id`` follow ``into_id``. Merged ids are never given to
    new entities. No foreign keys to ``entity``: the merged item is gone, and the record
    outlives the kept one, so a deleted kept item doesn't bring the merged one back.
    """

    __tablename__ = "entity_merge"

    merged_id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str]
    ingest_key: Mapped[str | None]
    into_id: Mapped[int]
    merged_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_entity_merge_key", "type", "ingest_key"),)


class EntityMergeResource(Base):
    """A file a merged item had from its theme (§13, #120): reading the file again finds
    the merged item (``ctx.entities_of``), so the theme doesn't make a new one for it."""

    __tablename__ = "entity_merge_resource"

    merged_id: Mapped[int] = mapped_column(
        ForeignKey("entity_merge.merged_id", ondelete=CASCADE), primary_key=True
    )
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete=CASCADE), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(primary_key=True)


class UserRelation(Base):
    """A relationship the user added or removed by hand (§9, #260): ``added`` true for one
    they made (a theme's ``unrelate`` leaves it), false for one they removed (a theme's
    ``relate`` doesn't bring it back). ``a_id``/``b_id`` are the relationship's two sides."""

    __tablename__ = "user_relation"

    name: Mapped[str] = mapped_column(primary_key=True)
    a_id: Mapped[int] = mapped_column(ForeignKey("entity.id", ondelete=CASCADE), primary_key=True)
    b_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True, index=True
    )
    added: Mapped[bool]
    at: Mapped[datetime] = mapped_column(default=utcnow)


LAST_POSITION = 1 << 30
"""Where an item of an ordered relationship without a position sorts: after the rest."""


class UserOrder(Base):
    """The user put ``b_id``'s items of ordered relationship ``name`` in order by hand
    (#317): scans then leave its positions alone (new items go last)."""

    __tablename__ = "user_order"

    name: Mapped[str] = mapped_column(primary_key=True)
    b_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True, index=True
    )
    at: Mapped[datetime] = mapped_column(default=utcnow)


class UserContains(Base):
    """Containment the user added or removed by hand (#329): ``added`` true for an item
    they put in a container (a theme's ``uncontain`` leaves it), false for one they took
    out (a theme's ``contain`` doesn't bring it back)."""

    __tablename__ = "user_contains"

    parent_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    child_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True, index=True
    )
    added: Mapped[bool]
    at: Mapped[datetime] = mapped_column(default=utcnow)


class CachedResponse(Base):
    """A response from an online service, by address (§9 *Online details*, #339): an
    identifier is looked up once, and again only when the user asks. ``status`` is the HTTP
    status (a service that doesn't know the identifier answers 404, which is kept too)."""

    __tablename__ = "online_response"

    url: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[int]
    body: Mapped[str]
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)


class OnlinePicture(Base):
    """A picture of an item from an online service (§9 *Online details*, #483): a book's
    cover, a movie's poster, a person's photo, named by the theme from what it looked up,
    kept here (never in the user's folders) and shown only when the item has no picture of
    its own. ``status`` and ``data`` are empty until it is fetched; a service without the
    picture answers 404, which is kept too, so it isn't asked again."""

    __tablename__ = "online_picture"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    url: Mapped[str]
    status: Mapped[int | None]
    data: Mapped[bytes | None]
    fetched_at: Mapped[datetime | None]


class TagPicture(Base):
    """A tag's picture (#506, §7 *Tag pictures*): a copy kept in the keep (PNG or JPEG, at
    most 480 px), shown above the tag's description in its tooltips."""

    __tablename__ = "tag_picture"

    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete=CASCADE), primary_key=True)
    data: Mapped[bytes]
    width: Mapped[int]
    height: Mapped[int]


class TagIconPicture(Base):
    """A tag's icon as a small picture (#507, §7 *Tag icons*), in place of an emoji: a copy
    kept in the keep (PNG, or an animated GIF), at most 64 px."""

    __tablename__ = "tag_icon_picture"

    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete=CASCADE), primary_key=True)
    data: Mapped[bytes]
    width: Mapped[int]
    height: Mapped[int]


class ArchiveMode(enum.StrEnum):
    """What the user chose to read inside one archive (#391)."""

    ALL = "all"
    CHOSEN = "chosen"
    NONE = "none"


class ArchiveRead(Base):
    """How an archive's members are read as resources (#391, DESIGN.md §6 *Archive
    members*), and when its listing was last read: a row per archive that has been looked
    inside, or that the user chose for.

    ``mode`` is the user's choice for this archive: ``all`` (every member, those added
    later too), ``chosen`` (the members marked read in ``archive_member_choice``), ``none``
    (none, over a folder pattern), or ``None`` (as the root's ``archives`` patterns say).
    ``listed_size`` and ``listed_mtime_ns`` are the archive's when its members were last
    listed: a scan lists it again only when they change, or when it has never been listed.
    """

    __tablename__ = "archive_read"

    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete=CASCADE), primary_key=True
    )
    mode: Mapped[ArchiveMode | None] = mapped_column(_enum(ArchiveMode))
    listed_size: Mapped[int | None]
    listed_mtime_ns: Mapped[int | None]


class ArchiveMemberChoice(Base):
    """What the user decided for one member of an archive read member by member (#391):
    ``read`` true (read it), false (hidden), or ``None`` (not decided yet: a member that
    appeared after the choice, listed for review in Triage, #520). ``path`` is the member's
    path inside the archive, with ``/``."""

    __tablename__ = "archive_member_choice"

    archive_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete=CASCADE), primary_key=True
    )
    path: Mapped[str] = mapped_column(primary_key=True)
    read: Mapped[bool | None]


class DedupeDismissal(Base):
    """A group of identical files or a pair of similar items the user marked as not a
    duplicate (§12 Dedupe, #121): hidden from the Dedupe page.

    ``kind`` is ``exact`` (``key`` the fingerprint in hex, ``marker`` the group's resource
    ids: it shows again once those change) or ``similar`` (``key`` the two entity ids,
    lower first). Rows naming things since gone are harmless and ignored.
    """

    __tablename__ = "dedupe_dismissal"

    kind: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    marker: Mapped[str] = mapped_column(default="")
    dismissed_at: Mapped[datetime] = mapped_column(default=utcnow)


class EntityNote(Base):
    """An item's note (§9 *Notes*, #379): where it is looked for (``folder_id``: the item's
    folder, or the one a theme named with ``ctx.note``), the file last read
    (``resource_id``, with its size and time then, so an unchanged note isn't read again),
    and its body. Re-read from the file at scans; a note with no file (``resource_id`` and
    ``folder_id`` both ``NULL``) is one typed in Tagalot (#383)."""

    __tablename__ = "entity_note"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("resource.id", ondelete="SET NULL"), index=True
    )
    resource_id: Mapped[int | None] = mapped_column(
        ForeignKey("resource.id", ondelete="SET NULL"), index=True
    )
    size: Mapped[int | None]
    mtime_ns: Mapped[int | None]
    body: Mapped[str | None] = mapped_column(Text)
    picture: Mapped[str | None]
    """The picture the note names (``cover``, else ``image``), as written (#439)."""
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ItemPicture(Base):
    """An item's own picture (#439), tried before its theme's thumbnail chain: one the user
    chose (``user``), and one its note names or that is beside its note in the notes folder
    (``note``, worked out at each scan by ``core.item_pictures``). An item can have both:
    the user's is used."""

    __tablename__ = "item_picture"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    source: Mapped[str] = mapped_column(primary_key=True)
    """``user`` or ``note``."""
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete=CASCADE), index=True
    )


class NoteKeyRemoval(Base):
    """An Extra field from a note that the user removed from that item (§9 *Notes*): later
    scans leave it off. ``key`` is case-folded."""

    __tablename__ = "note_key_removal"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    key: Mapped[str] = mapped_column(primary_key=True)


class NoteIgnoredKey(Base):
    """The ignored front-matter keys (case-folded, built-in ones included) that the keep's
    notes were last applied with (§9 *Notes*, #381): when the lists in ``settings.toml`` and
    ``keep.toml`` say otherwise, the next scan reads every note again."""

    __tablename__ = "note_ignored_key"

    key: Mapped[str] = mapped_column(primary_key=True)


class EntityField(Base):
    """A value of one of the keep's own fields (#581, ``keep.toml``'s ``[fields]``,
    ``core/own_values.py``): one row per item and field, the value in the column of its
    kind (text and links in ``text``; whole numbers and yes/no in ``integer``; numbers
    in ``real``; dates in ``day``), each indexed with the field's name for sorting and
    filtering. An empty value has no row."""

    __tablename__ = "entity_field"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    field: Mapped[str] = mapped_column(primary_key=True)
    text: Mapped[str | None] = mapped_column(Text)
    integer: Mapped[int | None] = mapped_column(Integer)
    real: Mapped[float | None] = mapped_column(Float)
    day: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        Index("ix_entity_field_text", "field", "text"),
        Index("ix_entity_field_integer", "field", "integer"),
        Index("ix_entity_field_real", "field", "real"),
        Index("ix_entity_field_day", "field", "day"),
    )


class OwnFieldRead(Base):
    """The keep's own fields (#584, ``keep.toml``'s ``[fields]``) that its notes and
    Markdown files were last read with: a key named like one fills that field. When
    ``keep.toml`` says otherwise, the next scan reads them all again (as for
    :class:`NoteKeyAlias`). Reading looks the fields up here."""

    __tablename__ = "own_field_read"

    type_id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(primary_key=True)
    label: Mapped[str]
    kind: Mapped[str]


class NoteKeyAlias(Base):
    """The user's front-matter key aliases (#411; the key case-folded, and the field's name
    or label as written) that the keep's notes and Markdown files were last read with: when
    the lists in ``settings.toml`` and ``keep.toml`` say otherwise, the next scan reads them
    again. Reading looks the aliases up here."""

    __tablename__ = "note_key_alias"

    key: Mapped[str] = mapped_column(primary_key=True)
    field: Mapped[str]


class NotesFolderNote(Base):
    """A note in the keep's notes folder (§9 *The notes folder*, #382), as last read: what
    it is matched by, and what it matched. Its item's ``entity_note`` holds what it gave.
    Notes matching no item, or several, and notes whose item has another note, are listed
    in Triage."""

    __tablename__ = "notes_folder_note"

    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete=CASCADE), primary_key=True
    )
    size: Mapped[int | None]
    mtime_ns: Mapped[int | None]
    title: Mapped[str]
    """The note's ``title``, else its file name without the extension."""
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    type: Mapped[str | None]
    """The entity type the note is for (``tagalot-type``, else its subfolder); ``None``:
    any."""
    matches: Mapped[int] = mapped_column(default=0)
    """How many items it matched when last matched."""
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("entity.id", ondelete="SET NULL"), index=True
    )
    """The item it matched, when it matched exactly one."""
    applied: Mapped[bool] = mapped_column(default=False)
    """It is that item's note (else the item has another one)."""


class FileNotFound(Base):
    """A file an item's file names that wasn't found, such as a Markdown book's cover
    (``IngestContext.not_found``, #426): listed in Triage with the paths tried. Replaced
    when the naming file is read again; not listed once the item has a file in ``role``."""

    __tablename__ = "file_not_found"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entity.id", ondelete=CASCADE), primary_key=True
    )
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete=CASCADE), primary_key=True, index=True
    )
    """The file that names it."""
    role: Mapped[str] = mapped_column(primary_key=True)
    written: Mapped[str]
    """What the file says, as written."""
    tried: Mapped[list[str]] = mapped_column(JSON, default=list)
    """The root-relative paths looked in, in order (none when ``reason`` says why not)."""
    reason: Mapped[str | None]
    """Why nothing was looked for: a web address, a path outside the watched folder."""


class PictureFailure(Base):
    """A picture that couldn't be drawn for a thumbnail (corrupt or unsupported, #426):
    listed in Triage for the items that have it. Ignored once the file changes; removed
    when it is drawn."""

    __tablename__ = "picture_failure"

    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete=CASCADE), primary_key=True
    )
    size: Mapped[int | None]
    mtime_ns: Mapped[int | None]
    """The file's size and time when it failed."""
    error: Mapped[str]


class MarkdownProblem(Base):
    """What is wrong with a Markdown file's front matter (#438): keys given twice, or
    front matter that doesn't parse. Replaced each time the file is read in a scan; listed
    in Triage → Markdown."""

    __tablename__ = "markdown_problem"

    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete=CASCADE), primary_key=True
    )
    problems: Mapped[list[str]] = mapped_column(JSON, default=list)


class PictureIgnore(Base):
    """A rule leaving rows off Triage's Pictures list (#426): one item's row while it says
    the same (``item``: ``entity_id`` and the row's ``marker``), a value files name, case
    aside, for every item (``value``), or every item of a type (``type``)."""

    __tablename__ = "picture_ignore"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str]
    entity_id: Mapped[int | None] = mapped_column(ForeignKey("entity.id", ondelete=CASCADE))
    marker: Mapped[str | None]
    value: Mapped[str | None]
    type: Mapped[str | None]


class SavedSearch(Base):
    """A named search; ``definition`` is ``core.saved_searches.SavedDefinition``'s JSON form:
    the page's search and its filter bar's, and how it was shown (§8, #127)."""

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
