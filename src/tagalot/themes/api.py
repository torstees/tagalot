"""Public, versioned theme API: the only module themes may import (DESIGN.md §9).

A theme is one Python module with one :class:`Theme` subclass. Everything here is a plain
declaration: importing a theme touches no database and no SQLAlchemy registry. When a keep
opens, the core turns these declarations into tables and runs the theme's ``ingest``.

This module depends only on the standard library, so the core (and the launcher, which
validates every theme) can import it freely.
"""

import enum
import fnmatch
import types
import typing
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar, Literal, Protocol, TypeVar

API_VERSION = 1
"""The version of this contract. It changes only with a DESIGN.md §9 update."""

FIELD_TYPES: tuple[type, ...] = (str, int, float, bool, date, datetime)
"""Python types a field may have, each optionally ``| None``."""

SearchKind = Literal["text", "range", "choice"]
SEARCH_KINDS: frozenset[str] = frozenset(typing.get_args(SearchKind))


class Kind(enum.StrEnum):
    """Resource kinds a :func:`role` can accept; the core classifies files by extension."""

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FONT = "font"
    ARCHIVE = "archive"
    DIR = "dir"
    ANY = "any"


KIND_EXTENSIONS: Mapping[Kind, frozenset[str]] = {
    Kind.IMAGE: frozenset(
        {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".psd", ".tga"}
    ),
    Kind.AUDIO: frozenset(
        {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".aif", ".aiff"}
    ),
    Kind.VIDEO: frozenset({".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".mpg"}),
    Kind.FONT: frozenset({".ttf", ".otf", ".woff", ".woff2"}),
    Kind.ARCHIVE: frozenset({".zip", ".7z", ".rar", ".cbz", ".cbr", ".cb7"}),
}
"""File extensions of each resource kind (lowercase, with the dot)."""


class ThemeDeclarationError(ValueError):
    """A theme declaration is invalid; the message says what to fix."""


# --- Fields ---


@dataclass
class FieldSpec:
    """A typed field of an entity, declared with :func:`field`.

    ``name`` is filled in from the attribute name; the type comes from the annotation.
    """

    label: str
    card: bool = False
    """Show on the entity's card in grid and list layouts."""
    search: SearchKind | None = None
    """How the field can be filtered: ``"text"``, ``"range"``, ``"choice"``, or not at all."""
    editable: bool = True
    detail: bool = True
    """Show in the detail page's fields section."""
    name: str = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name


def field(
    label: str,
    *,
    card: bool = False,
    search: SearchKind | None = None,
    editable: bool = True,
    detail: bool = True,
) -> Any:
    """Declare a field: ``year: int | None = field("Year", card=True, search="range")``.

    Typed ``Any`` so the declaration type-checks against its annotation.
    """
    if search is not None and search not in SEARCH_KINDS:
        raise ThemeDeclarationError(
            f"search={search!r} is not one of {', '.join(sorted(SEARCH_KINDS))}"
        )
    if not label.strip():
        raise ThemeDeclarationError("a field needs a label")
    return FieldSpec(label, card=card, search=search, editable=editable, detail=detail)


@dataclass(frozen=True)
class FieldInfo:
    """A field as the core sees it: spec plus resolved type."""

    name: str
    type: type
    """One of :data:`FIELD_TYPES`."""
    nullable: bool
    spec: FieldSpec


# --- Roles ---


@dataclass(frozen=True)
class Role:
    """A function a resource plays for an entity, declared with :func:`role`."""

    name: str
    kinds: frozenset[Kind]
    many: bool = False
    primary: bool = False
    """The entity's main content; defines "open file". At most one per type."""
    thumbnail: bool = False
    """Tried early when resolving the entity's thumbnail."""
    label: str | None = None


def role(
    name: str,
    *,
    kinds: Iterable[str],
    many: bool = False,
    primary: bool = False,
    thumbnail: bool = False,
    label: str | None = None,
) -> Role:
    """Declare a role: ``role("poster", kinds={"image"}, thumbnail=True)``."""
    try:
        parsed = frozenset(Kind(k) for k in kinds)
    except ValueError as e:
        allowed = ", ".join(k.value for k in Kind)
        raise ThemeDeclarationError(f"role {name!r}: {e}; kinds are {allowed}") from None
    if not parsed:
        raise ThemeDeclarationError(f"role {name!r} needs at least one kind")
    return Role(name, parsed, many=many, primary=primary, thumbnail=thumbnail, label=label)


# --- Entities ---


class Entity:
    """Base for entity declarations. Subclasses declare fields and roles; they are never
    instantiated and never mapped by SQLAlchemy.

    Class attributes a subclass may set:

    - ``label`` / ``plural``: how the type is named in the UI ("Album" / "Albums"). The
      defaults are the class name and a regular English plural of the label (see
      :func:`entity_plural`); set ``plural`` for irregular words ("Series", "People").
    - ``title_label``: what the core ``title`` is called for this type (default "Title").
    - ``roles``: a list of :func:`role` declarations.
    - ``double_click``: ``"page"`` (open the detail page, the default) or ``"open_file"``.
    - ``table_name`` / ``type_id``: override the defaults ``<theme id>_<class name>`` and
      ``<theme id>.<class name>`` (lowercased).
    """

    label: ClassVar[str | None] = None
    plural: ClassVar[str | None] = None
    title_label: ClassVar[str] = "Title"
    roles: ClassVar[Sequence[Role]] = ()
    double_click: ClassVar[Literal["page", "open_file"]] = "page"
    table_name: ClassVar[str | None] = None
    type_id: ClassVar[str | None] = None


def entity_label(entity: type[Entity]) -> str:
    """The type's display name: its ``label``, or the class name."""
    return entity.label or entity.__name__


def entity_plural(entity: type[Entity]) -> str:
    """The type's plural display name: its ``plural``, or a regular English plural of its
    label ("Album" -> "Albums", "Box" -> "Boxes", "Category" -> "Categories"). Words the
    rules get wrong ("Series", "Person") need an explicit ``plural``."""
    if entity.plural:
        return entity.plural
    return plural_of(entity_label(entity))


def plural_of(word: str) -> str:
    """A regular English plural of ``word``, applied to its last letters."""
    lower = word.lower()
    if lower.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    if lower.endswith("y") and len(word) > 1 and lower[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def entity_fields(entity: type[Entity]) -> list[FieldInfo]:
    """The declared fields of an entity class, in declaration order, with resolved types.

    Raises :class:`ThemeDeclarationError` for a field without a supported annotation.
    """
    try:
        hints = typing.get_type_hints(entity)
    except Exception as e:
        raise ThemeDeclarationError(f"{entity.__name__}: can't read field types ({e})") from e
    fields = []
    for klass in reversed(entity.__mro__):
        for name, value in vars(klass).items():
            if not isinstance(value, FieldSpec):
                continue
            if name not in hints:
                raise ThemeDeclarationError(f"{entity.__name__}.{name} needs a type annotation")
            python_type, nullable = _field_type(hints[name])
            if python_type is None:
                allowed = ", ".join(t.__name__ for t in FIELD_TYPES)
                raise ThemeDeclarationError(
                    f"{entity.__name__}.{name}: {hints[name]!r} is not a field type ({allowed})"
                )
            fields.append(FieldInfo(name, python_type, nullable, value))
    return list({f.name: f for f in fields}.values())  # subclasses override by name


def _field_type(hint: Any) -> tuple[type | None, bool]:
    if hint in FIELD_TYPES:
        return hint, False
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(hint) if a is not type(None)]
        if len(args) == 1 and len(typing.get_args(hint)) == 2 and args[0] in FIELD_TYPES:
            return args[0], True
    return None, False


# --- Containment and relationships ---


@dataclass(frozen=True)
class Containment:
    """``parent`` entities contain ``child`` entities (Artist ⊃ Album)."""

    parent: type[Entity]
    child: type[Entity]


def contains(parent: type[Entity], child: type[Entity]) -> Containment:
    """Declare containment: ``contains(Album, Song)``. The ingester creates the edges."""
    return Containment(parent, child)


@dataclass(frozen=True)
class Relationship:
    """A non-containment link between two types (Actor ↔ Movie "cast")."""

    name: str
    a: type[Entity]
    b: type[Entity]
    label: str | None = None
    reverse_label: str | None = None
    many: bool = True
    """``False`` limits each ``b`` to at most one ``a``."""


def related(
    name: str,
    a: type[Entity],
    b: type[Entity],
    *,
    label: str | None = None,
    reverse_label: str | None = None,
    many: bool = True,
) -> Relationship:
    """Declare a relationship: ``related("cast", Actor, Movie, label="Cast")``.

    The core builds a link table ``<theme id>_<name>``.
    """
    if not name.isidentifier():
        raise ThemeDeclarationError(f"relationship name {name!r} must be an identifier")
    return Relationship(name, a, b, label, reverse_label, many)


# --- Views ---

Layout = Literal["grid", "list", "tree"]


@dataclass(frozen=True)
class SortBy:
    """A default sort for a view: a field name (or ``"title"``), optionally descending."""

    field: str
    descending: bool = False


@dataclass(frozen=True)
class SearchView:
    """A scoped search preset in the navigation (§8): ``SearchView("Albums", [Album])``."""

    name: str
    types: Sequence[type[Entity]]
    inherit_tags: bool = False
    show_contained: bool = False
    layout: Layout = "grid"
    default_sort: Sequence[SortBy] = (SortBy("title"),)


@dataclass(frozen=True)
class Section:
    """A block of a detail page. Build one with the class methods."""

    kind: Literal["fields", "role", "gallery", "related", "contents", "custom"]
    name: str | None = None
    factory: Callable[[Any], Any] | None = None

    @classmethod
    def fields(cls) -> "Section":
        """The entity's fields, with inline editing."""
        return cls("fields")

    @classmethod
    def role(cls, name: str) -> "Section":
        """A role slot: its file(s), and a drop target for linking more."""
        return cls("role", name)

    @classmethod
    def gallery(cls, name: str) -> "Section":
        """A thumbnail gallery of a many-valued image role."""
        return cls("gallery", name)

    @classmethod
    def related(cls, name: str) -> "Section":
        """Entities linked through a :func:`related` relationship, from either side."""
        return cls("related", name)

    @classmethod
    def contents(cls) -> "Section":
        """For containers: an embedded search of what the entity contains."""
        return cls("contents")

    @classmethod
    def custom(cls, factory: Callable[[Any], Any]) -> "Section":
        """Escape hatch: ``factory(context)`` returns a Qt widget. The theme imports Qt
        itself; this module does not."""
        return cls("custom", factory=factory)


@dataclass(frozen=True)
class DetailView:
    """The detail page of one entity type: its sections, top to bottom."""

    type: type[Entity]
    sections: Sequence[Section]


View = SearchView | DetailView


# --- Actions ---

_F = TypeVar("_F", bound=Callable[..., Any])


@dataclass(frozen=True)
class ActionSpec:
    label: str
    applies_to: tuple[type[Entity] | str, ...]
    """Entity types, or role names (the action applies to entities with that role)."""


def action(label: str, applies_to: Iterable[type[Entity] | str]) -> Callable[[_F], _F]:
    """Mark a :class:`Theme` method as a context-menu and detail-page action.

    The method is called as ``method(entities, ctx)``. Actions must not modify user files.
    """
    spec = ActionSpec(label, tuple(applies_to))

    def mark(method: _F) -> _F:
        method.__tagalot_action__ = spec  # type: ignore[attr-defined]
        return method

    return mark


# --- Values themes receive ---


@dataclass(frozen=True)
class EntityRef:
    """An opaque handle to an entity; themes never hold database objects."""

    id: int
    type: str


@dataclass(frozen=True)
class Record:
    """Read-only field values of an entity, from :meth:`IngestContext.get`."""

    ref: EntityRef
    title: str
    fields: Mapping[str, Any]
    extra: Mapping[str, Any]


@dataclass(frozen=True)
class ResourceInfo:
    """A file or folder handed to :meth:`Theme.ingest`. Read it; never write under a root."""

    id: int
    root_id: str
    relpath: str
    """POSIX-style path relative to the root."""
    kind: Literal["file", "dir"]
    ext: str
    size: int | None
    mtime_ns: int | None
    path: str
    """This machine's path to the file, for reading."""


class IngestContext(Protocol):
    """What ``ingest``, actions, and ``migrate`` can do (DESIGN.md §9 "Ingest context").

    All writes go through the core's DB writer, and extracted values never overwrite fields
    the user edited.
    """

    def upsert(
        self, type: type[Entity], key: str, *, title: str | None = None, **fields: Any
    ) -> EntityRef:
        """Find the entity of this type with this ingest key, or create it; set fields."""
        ...

    def link(
        self, entity: EntityRef, resource: ResourceInfo | int, role: str, sort_order: int = 0
    ) -> None: ...

    def unlink(self, entity: EntityRef, resource: ResourceInfo | int, role: str) -> None: ...

    def contain(self, parent: EntityRef, child: EntityRef) -> None: ...

    def uncontain(self, parent: EntityRef, child: EntityRef) -> None: ...

    def relate(self, name: str, a: EntityRef, b: EntityRef) -> None: ...

    def unrelate(self, name: str, a: EntityRef, b: EntityRef) -> None: ...

    def update(self, entity: EntityRef, *, title: str | None = None, **fields: Any) -> None:
        """Set extracted values on a known entity; user-edited values are left alone."""
        ...

    def entities_of(self, resource: ResourceInfo | int, role: str | None = None) -> list[EntityRef]:
        """Entities linked to a resource (in ``role``, if given). After a file is moved, its
        links follow it, so this is how file-based themes find "the entity for this file"."""
        ...

    def find(self, type: type[Entity], **equals: Any) -> list[EntityRef]: ...

    def get(self, entity: EntityRef) -> Record: ...

    def warn(self, resource: ResourceInfo | None, message: str) -> None:
        """Report a problem with a file to the activity panel."""
        ...


def kind_of(resource: ResourceInfo) -> Kind | None:
    """What kind of resource this is: ``Kind.DIR`` for folders, else by file extension;
    ``None`` for files of no known kind."""
    if resource.kind == "dir":
        return Kind.DIR
    return next((k for k, exts in KIND_EXTENSIONS.items() if resource.ext in exts), None)


# --- Thumbnails ---


class ThumbnailContext(Protocol):
    """What thumbnail providers can look up (read-only; DESIGN.md §10)."""

    def resources(self, entity: EntityRef, role: str | None = None) -> list[ResourceInfo]:
        """Resources linked to ``entity`` (in ``role``, if given), in their sort order.
        Files known to be missing are left out."""
        ...

    def primary_role(self, entity: EntityRef) -> str | None:
        """The name of the entity type's primary role, if it has one."""
        ...

    def parents(self, entity: EntityRef) -> list[EntityRef]:
        """The entities that directly contain ``entity``."""
        ...

    def children(self, entity: EntityRef) -> list[EntityRef]:
        """The entities ``entity`` directly contains, by title."""
        ...

    def folder_files(
        self, entity: EntityRef, extensions: Iterable[str] | None = None
    ) -> list[ResourceInfo]:
        """Files directly inside the entity's folder, by name: the folder of its primary
        resource, or that resource itself when it is a folder. Only scanned files are known,
        so the theme's ``extensions`` must include the ones wanted. ``extensions`` narrows
        the list (lowercase, with the dot)."""
        ...

    def thumbnail_of(self, entity: EntityRef) -> ResourceInfo | None:
        """The resource another entity's thumbnail comes from, resolving it if needed;
        ``None`` when it shows an icon."""
        ...


class ThumbnailProvider:
    """One step of an entity type's thumbnail chain (:meth:`Theme.thumbnail_chain`).

    A provider only *chooses* resources. The core turns a resource into a picture by its
    kind (an image is decoded, an archive shows its first image, an audio file its embedded
    art) and tries the next candidate, then the next provider, when that fails. Subclass it
    to choose resources your own way.
    """

    id: ClassVar[str] = "provider"
    """Names the provider in logs and error reports."""

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        """Resources to try for ``entity``'s thumbnail, best first."""
        return ()


class RoleImage(ThumbnailProvider):
    """The resources linked in a role, such as ``poster`` or ``photo``."""

    id = "role"

    def __init__(self, role: str) -> None:
        self.role = role

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        return ctx.resources(entity, self.role)

    def __repr__(self) -> str:
        return f"RoleImage({self.role!r})"


class ImageFile(ThumbnailProvider):
    """The entity's own file: the resources of its primary role, drawn by their kind (so an
    audio file shows its embedded art and an archive its first image)."""

    id = "image_file"

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        primary = ctx.primary_role(entity)
        return ctx.resources(entity, primary) if primary else ()

    def __repr__(self) -> str:
        return "ImageFile()"


class ParentThumbnail(ThumbnailProvider):
    """Whatever the containing entity (a song's album) shows."""

    id = "parent"

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        for parent in ctx.parents(entity):
            found = ctx.thumbnail_of(parent)
            if found is not None:
                yield found

    def __repr__(self) -> str:
        return "ParentThumbnail()"


FOLDER_IMAGE_NAMES: tuple[str, ...] = (
    "folder",
    "cover",
    "front",
    "albumart",
    "albumart_*_large",
    "albumartsmall",
)
"""File names (without extension, case-insensitive, ``*`` as a wildcard) that
:class:`FolderImage` looks for, in order of preference."""

FOLDER_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp"})
"""Extensions :class:`FolderImage` accepts."""


class FolderImage(ThumbnailProvider):
    """A picture in the entity's folder named by convention (``folder.jpg``, ``cover.png``,
    ``AlbumArt_{…}_Large.jpg``…). Pass ``names`` to extend the list, for example
    ``FolderImage([*FOLDER_IMAGE_NAMES, "poster"])``."""

    id = "folder_image"

    def __init__(self, names: Iterable[str] = FOLDER_IMAGE_NAMES) -> None:
        self.names = tuple(n.lower() for n in names)

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        files = ctx.folder_files(entity, FOLDER_IMAGE_EXTENSIONS)
        stems = [(f.relpath.rpartition("/")[2].rpartition(".")[0].lower(), f) for f in files]
        for pattern in self.names:
            for stem, file in stems:
                if fnmatch.fnmatchcase(stem, pattern):
                    yield file

    def __repr__(self) -> str:
        return (
            "FolderImage()"
            if self.names == FOLDER_IMAGE_NAMES
            else f"FolderImage({list(self.names)!r})"
        )


class EmbeddedAudioArt(ThumbnailProvider):
    """Cover art embedded in the entity's audio (ID3, FLAC, MP4, Ogg pictures); for an
    entity without audio of its own, such as an album, that of its first contained items."""

    id = "embedded_audio_art"

    children_tried = 3
    """How many contained items to try: each try reads a file."""

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        own = _primary_of_kind(entity, ctx, Kind.AUDIO)
        if own:
            yield from own
            return
        tried = 0
        for child in ctx.children(entity):
            audio = _primary_of_kind(child, ctx, Kind.AUDIO)
            if audio:
                yield audio[0]
                tried += 1
                if tried == self.children_tried:
                    return

    def __repr__(self) -> str:
        return "EmbeddedAudioArt()"


class ArchiveFirstImage(ThumbnailProvider):
    """The best image inside the entity's archive (zip, 7z, rar): one named like a cover or
    preview, else the first in natural order (DESIGN.md §10 "Archives")."""

    id = "archive_first_image"

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        return _primary_of_kind(entity, ctx, Kind.ARCHIVE)

    def __repr__(self) -> str:
        return "ArchiveFirstImage()"


def _primary_of_kind(entity: EntityRef, ctx: ThumbnailContext, kind: Kind) -> list[ResourceInfo]:
    primary = ctx.primary_role(entity)
    if primary is None:
        return []
    return [r for r in ctx.resources(entity, primary) if kind_of(r) is kind]


class Icon(ThumbnailProvider):
    """A generic icon; ends the chain. ``name`` is a :class:`Kind` value (``"audio"``,
    ``"image"``…), ``"file"``, or ``"entity"``; unknown names show the generic icon."""

    id = "icon"

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"Icon({self.name!r})"


def default_thumbnail_chain(entity: type[Entity]) -> list[ThumbnailProvider]:
    """The chain :meth:`Theme.thumbnail_chain` uses unless a theme overrides it: the roles
    marked ``thumbnail=True``, then the entity's own file, then an icon for its kind."""
    chain: list[ThumbnailProvider] = [RoleImage(r.name) for r in entity.roles if r.thumbnail]
    primary = next((r for r in entity.roles if r.primary), None)
    if primary is not None and not primary.thumbnail:
        chain.append(ImageFile())
    if primary is None:
        icon = "entity"
    else:
        kinds = primary.kinds - {Kind.ANY}
        icon = next(iter(kinds)).value if len(kinds) == 1 else "file"
    chain.append(Icon(icon))
    return chain


# --- The theme ---

DirRule = bool | Callable[[str], bool]


class Theme:
    """Base for themes. A theme module defines exactly one subclass.

    Class attributes: ``id`` (lowercase identifier), ``name``, ``version`` (the theme's
    schema version), ``api_version``, ``extensions`` (accepted file extensions, lowercase,
    with the dot; empty = all), ``dirs`` (whether folders become resources: ``bool`` or a
    predicate on the relative path), ``entities``, ``containment``, ``relationships``,
    ``views``.
    """

    id: ClassVar[str]
    name: ClassVar[str]
    version: ClassVar[int] = 1
    api_version: ClassVar[int] = API_VERSION
    extensions: ClassVar[frozenset[str] | set[str]] = frozenset()
    dirs: ClassVar[DirRule] = False
    entities: ClassVar[Sequence[type[Entity]]] = ()
    containment: ClassVar[Sequence[Containment]] = ()
    relationships: ClassVar[Sequence[Relationship]] = ()
    views: ClassVar[Sequence[View]] = ()

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        """Turn new and changed resources into entities, links, containment, and fields."""

    def migrate(self, from_version: int, ctx: IngestContext) -> None:
        """Upgrade this theme's *data* from ``from_version`` to :attr:`version`.

        Runs after the user confirms, with ``keep.db`` backed up, in one transaction. Additive
        schema changes (new entity types, new fields) are applied by the core before this is
        called, so a theme that only adds things needs no ``migrate`` at all; the default does
        nothing.
        """

    def thumbnail_chain(self, entity_type: type[Entity]) -> Sequence[ThumbnailProvider]:
        """The providers tried, in order, for thumbnails of ``entity_type`` (DESIGN.md §10).

        The first resource that makes a picture wins; an :class:`Icon` ends the chain. The
        default is :func:`default_thumbnail_chain`.
        """
        return default_thumbnail_chain(entity_type)

    # --- Introspection (used by the core) ---

    @classmethod
    def type_id_of(cls, entity: type[Entity]) -> str:
        """``entity``'s namespaced type id, e.g. ``music.song``."""
        return entity.type_id or f"{cls.id}.{entity.__name__.lower()}"

    @classmethod
    def table_name_of(cls, entity: type[Entity]) -> str:
        """``entity``'s table name, e.g. ``music_song``."""
        return entity.table_name or f"{cls.id}_{entity.__name__.lower()}"

    @classmethod
    def actions(cls) -> dict[str, ActionSpec]:
        """Method name -> action declaration, for methods marked with :func:`action`."""
        found: dict[str, ActionSpec] = {}
        for klass in reversed(cls.__mro__):
            for name, value in vars(klass).items():
                spec = getattr(value, "__tagalot_action__", None)
                if isinstance(spec, ActionSpec):
                    found[name] = spec
        return found


__all__ = [
    "API_VERSION",
    "FIELD_TYPES",
    "FOLDER_IMAGE_EXTENSIONS",
    "FOLDER_IMAGE_NAMES",
    "KIND_EXTENSIONS",
    "SEARCH_KINDS",
    "ActionSpec",
    "ArchiveFirstImage",
    "Containment",
    "DetailView",
    "EmbeddedAudioArt",
    "Entity",
    "EntityRef",
    "FieldInfo",
    "FieldSpec",
    "FolderImage",
    "Icon",
    "ImageFile",
    "IngestContext",
    "Kind",
    "ParentThumbnail",
    "Record",
    "Relationship",
    "ResourceInfo",
    "Role",
    "RoleImage",
    "SearchView",
    "Section",
    "SortBy",
    "Theme",
    "ThemeDeclarationError",
    "ThumbnailContext",
    "ThumbnailProvider",
    "View",
    "action",
    "contains",
    "default_thumbnail_chain",
    "entity_fields",
    "entity_label",
    "entity_plural",
    "field",
    "kind_of",
    "plural_of",
    "related",
    "role",
]
