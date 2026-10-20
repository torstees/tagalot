"""Public, versioned theme API: the only module themes may import (DESIGN.md §9).

A theme is one Python module with one :class:`Theme` subclass. Everything here is a plain
declaration: importing a theme touches no database and no SQLAlchemy registry. When a keep
opens, the core turns these declarations into tables and runs the theme's ``ingest``.

This module depends only on the standard library, so the core (and the launcher, which
validates every theme) can import it freely.
"""

import enum
import fnmatch
import json
import os
import posixpath
import re
import types
import typing
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar, Literal, Protocol, TypeVar

API_VERSION = 21
"""The version of this contract. It changes only with a DESIGN.md §9 update. Version 2
added :meth:`Theme.migrate_schema`; version 3 added :meth:`IngestContext.resource_at` and
the file readers (:func:`read_pdf_info`, :func:`read_front_matter`, …), the
``"url"`` field display, and writing back to files (:attr:`Theme.write_back`,
:meth:`Theme.front_matter`); version 4 added ordered relationships (:func:`related`'s
``ordered``, :meth:`IngestContext.relate`'s ``position``), :attr:`Theme.read_last`,
:func:`read_bibliography`, and :attr:`Entity.made_by_hand`; version 5 added online
details (:attr:`Theme.online_sources`, :meth:`Theme.online_requests`,
:meth:`Theme.online_details`); version 6 added searching inside documents
(:attr:`Entity.full_text`, :meth:`Theme.document_text`, :func:`read_document_text`);
version 7 added Kindle and Mobipocket books (:func:`read_mobi`, :func:`mobi_cover`, and
their text in :func:`read_document_text`); version 8 added notes
(:meth:`IngestContext.note`); version 9 added assets folders (``assets`` of
:meth:`IngestContext.resource_at`); version 10 added title cards (:class:`TitleCard`);
version 11 added Extra fields from files (:meth:`IngestContext.extra`, and
:func:`read_front_matter`'s ``other``); version 12 added descriptions
(:attr:`Entity.description_field`); version 13 added reporting files not found
(:meth:`IngestContext.not_found`); version 14 added gallery roles filled from folders
(:func:`role`'s ``gallery`` and ``folders``); version 15 added fields that mirror links
(:func:`field`'s ``mirrors`` and ``many``, :meth:`Theme.item_named`); version 16 added
ratings shown as stars (:func:`field`'s ``rating``); version 17 added pictures' and
archives' details and dates (:func:`read_picture`, :func:`read_archive`, :func:`oldest`);
version 18 added folder items (:attr:`Entity.folders_for`); version 19 added gallery roles
filled from the item's own folder (:func:`role`'s ``own_folder``); version 20 added
see-through folders (:attr:`ResourceInfo.folder`, :attr:`ResourceInfo.folder_path`,
:attr:`ResourceInfo.see_through`, :func:`files_in`); version 21 added pictures from
online services (:meth:`IngestContext.online_picture`)."""

FIELD_TYPES: tuple[type, ...] = (str, int, float, bool, date, datetime)
"""Python types a field may have, each optionally ``| None``."""

SearchKind = Literal["text", "range", "choice"]
SEARCH_KINDS: frozenset[str] = frozenset(typing.get_args(SearchKind))

DISPLAY_FORMATS: frozenset[str] = frozenset({"bytes", "duration", "url"})
"""How a field may be shown (:func:`field`'s ``display``): ``"bytes"`` and ``"duration"``
for number fields, ``"url"`` for text fields."""

RATING_SCALES: frozenset[int] = frozenset({5, 10})
"""What a rating may be out of (:func:`field`'s ``rating``): 5, or 10 (a movie's score)."""


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
    display: str | None = None
    """How the value reads: ``"bytes"`` (3.0 MB) or ``"duration"`` (seconds as 3:25) for a
    number, ``"url"`` (a link that opens in the browser) for text; ``"stars/5"`` or
    ``"stars/10"`` for a rating (set by ``rating``)."""
    rating: int | None = None
    """What this rating is out of (API version 16): 5 or 10. It shows as five stars."""
    mirrors: "type[Entity] | str | None" = None
    """The link this text field is a copy of (API version 15): a container type
    (``Series``: the titles of the item's containers of that type, by a containment the
    theme declares) or a relationship's name (``"writers"``: the items on its other side).
    Editing the field changes the links."""
    many: bool = False
    """A mirrored containment field lists several containers, comma-separated (a book's
    collections); a relationship's own ``many`` says so for it."""
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
    display: str | None = None,
    mirrors: "type[Entity] | str | None" = None,
    many: bool = False,
    rating: int | None = None,
) -> Any:
    """Declare a field: ``year: int | None = field("Year", card=True, search="range")``.

    ``display`` formats a field for people: on a number field, ``"bytes"`` shows a size as
    ``3.0 MB`` and ``"duration"`` shows seconds as ``3:25``; on a text field, ``"url"``
    shows a web address (``http`` or ``https``) as a link that opens in the browser on the
    item's page (API version 3). Sorting, filtering, and editing still use the value.

    ``mirrors`` makes a text field a copy of a link (API version 15): a container type, for
    a containment the theme declares (``series: str | None = field("Series",
    mirrors=Series)`` on Book, with ``contains(Series, Book)``), or a relationship's name
    (``authors = field("Author", mirrors="writers")``). Your ``ingest``
    still writes both the field and the link from the files, as before; what changes is
    editing: the user types a name (or, with ``many=True`` for a containment, or a
    relationship that has many, a comma-separated list), and the core finds or makes the
    items with :meth:`Theme.item_named`, moves the item's links by hand (scans respect
    them), and keeps the field as the user's. A containment field that the item gets
    through another container (a book's universe through its series, when the theme
    declares ``contains(Universe, Series)``) can't be edited on the item: it follows the
    container.

    ``rating`` makes a ``float`` field a rating out of 5 or 10 (API version 16):
    ``rating: float | None = field("Rating", search="range", rating=5)``. It shows as five
    stars in lists, on cards, and on the item's page, in half stars (3.5 out of 5 is
    ★★★½☆; out of 10, each point is a half star), and the item's page sets it by clicking
    a star (its left half for the half). Searching a range uses the numbers. Use 5 unless
    the files score out of 10 (a movie's ``.nfo``). Typed ``Any`` so the declaration
    type-checks against its annotation.
    """
    if rating is not None:
        if rating not in RATING_SCALES:
            raise ThemeDeclarationError(f"rating={rating!r} must be 5 or 10")
        if display is not None:
            raise ThemeDeclarationError("a rating shows as stars: it takes no display")
        display = f"stars/{rating}"
    elif display is not None and display not in DISPLAY_FORMATS:
        raise ThemeDeclarationError(
            f"display={display!r} is not one of {', '.join(sorted(DISPLAY_FORMATS))}"
        )
    if search is not None and search not in SEARCH_KINDS:
        raise ThemeDeclarationError(
            f"search={search!r} is not one of {', '.join(sorted(SEARCH_KINDS))}"
        )
    if not label.strip():
        raise ThemeDeclarationError("a field needs a label")
    return FieldSpec(
        label,
        card=card,
        search=search,
        editable=editable,
        detail=detail,
        display=display,
        mirrors=mirrors,
        many=many,
        rating=rating,
    )


@dataclass(frozen=True)
class FieldInfo:
    """A field as the core sees it: spec plus resolved type."""

    name: str
    type: type
    """One of :data:`FIELD_TYPES`."""
    nullable: bool
    spec: FieldSpec


# --- Options ---

OPTION_TYPES: tuple[type, ...] = (bool, int, float, str)
"""Types a theme option may have."""


@dataclass(frozen=True)
class ThemeOption:
    """A setting of a theme that a keep (or one of its roots) can change, declared with
    :func:`option`."""

    name: str
    type: type
    default: Any
    label: str
    description: str = ""


def option(name: str, default: Any, *, label: str, description: str = "") -> ThemeOption:
    """Declare a theme option; its type is the default's (``bool``, ``int``, ``float``, or
    ``str``): ``option("artist_level", 1, label="Artist folder level")``.

    Keeps set options in ``keep.toml`` under ``[theme.options]``, and a root can override
    them with ``options = { … }``. :meth:`IngestContext.option` reads the value for the root
    being scanned; when a root's values change, its files are ingested again.
    """
    kind = type(default)
    if kind not in OPTION_TYPES:
        raise ThemeDeclarationError(
            f"option {name!r}: the default must be a bool, int, float, or str, not {default!r}"
        )
    if not name.isidentifier():
        raise ThemeDeclarationError(f"option name {name!r} must be a Python identifier")
    return ThemeOption(name, kind, default, label, description)


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
    gallery: bool = False
    """Pictures that belong to the item, shown as a gallery (API version 14)."""
    folders: tuple[str, ...] = ()
    """Subfolders of the item's folder the core fills a gallery role from (API version
    14)."""
    own_folder: bool = False
    """The core fills the gallery role from everything in the item's own folder (API
    version 19)."""


def role(
    name: str,
    *,
    kinds: Iterable[str],
    many: bool = False,
    primary: bool = False,
    thumbnail: bool = False,
    label: str | None = None,
    gallery: bool = False,
    folders: Iterable[str] = (),
    own_folder: bool = False,
) -> Role:
    """Declare a role: ``role("poster", kinds={"image"}, thumbnail=True)``.

    A **gallery** role (``gallery=True``, which needs ``many=True`` and the ``image``
    kind, with ``archive`` too to take archives) holds pictures that belong to the item:
    a movie's screenshots, an album's scanned booklet. With ``folders=("Screenshots",
    "Screens")`` the core fills it (DESIGN.md §9 *Galleries*, API version 14): every
    picture (and, with the ``archive`` kind, archive) under those subfolders of the item's
    folder, at any depth, and archives of those names beside them (``Screenshots.zip``).
    The item's folder is its primary role's folder, else the folder of its primary file.
    Names are compared ignoring case. Scans add and remove these links as pictures come
    and go; your theme's own links in the role, and the user's, stay.

    With ``own_folder=True`` instead (API version 19), the core fills it with every picture
    (and archive) in the item's own folder, at any depth: the folder of its primary file,
    when no other item of its type has a primary file in it or below it (a story's folder,
    with its renders), never the top of a watched folder. Files the item has in its other
    roles (its cover) aren't added.
    """
    try:
        parsed = frozenset(Kind(k) for k in kinds)
    except ValueError as e:
        allowed = ", ".join(k.value for k in Kind)
        raise ThemeDeclarationError(f"role {name!r}: {e}; kinds are {allowed}") from None
    if not parsed:
        raise ThemeDeclarationError(f"role {name!r} needs at least one kind")
    if isinstance(folders, str):
        folders = (folders,)
    return Role(
        name,
        parsed,
        many=many,
        primary=primary,
        thumbnail=thumbnail,
        label=label,
        gallery=gallery,
        folders=tuple(str(f) for f in folders),
        own_folder=own_folder,
    )


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
    - ``card_lines``: field names shown, in order, under the title on grid cards (default:
      none, just the title). Users can choose other lines per view.
    - ``contents_sort``: for a container, the order of what it holds, in the tree layout and
      on its page, as :class:`SortBy` keys naming fields of the types it can contain
      (``(SortBy("disc"), SortBy("track"), SortBy("title"))`` for an album). Items without
      a field sort first on it. Default: by title.
    - ``table_name`` / ``type_id``: override the defaults ``<theme id>_<class name>`` and
      ``<theme id>.<class name>`` (lowercased).
    - ``made_by_hand``: the user makes items of this type (a reading list, a playlist) and
      puts items in them (API version 4): Tagalot offers **New …** in its views, **Add to
      …** on the items it can contain (its containment), and **Remove from …** on its
      page. Scans never undo what the user put in or took out.
    - ``full_text``: items of this type are documents (a paper, a book): when the keep
      searches inside documents, the text of the files in the type's primary role is read
      in the background after scans and can be searched (DESIGN.md §8 *Search inside
      documents*; API version 6). The core reads them with :func:`read_document_text`, or
      :meth:`Theme.document_text`. It needs a primary role.
    - ``description_field``: the name of the text field that describes an item of this type
      (a book's ``"description"``, a paper's ``"abstract"``; API version 12). Tagalot shows
      it under the selected item in the preview strip, in grid cards' tooltips, and as a
      list column shown by default. Default: none.
    - ``folders_for``: makes this type the theme's **folder items** (API version 18; at
      most one type, with a primary ``dir`` role, in a theme with ``dirs = True``). After
      each scan the core makes one for every folder above a file of the types listed (the
      folder it's in, and each folder above it, up to the top of the watched folder), except
      a folder that is already another item's primary folder (a collection's), and takes
      away those no longer wanted (unless the user tagged them). Each one holds the items
      the theme lets it contain (``contains(Folder, Movie)``…) whose primary files are
      nearest under it, and the folder items under it, so a tag on a folder counts for
      everything inside in views with ``inherit_tags``. What the user puts in or takes out
      by hand stays. ``folders_for = (Picture, Archive)``: folders of pictures (DESIGN.md
      §9 *Folder items*).
    """

    label: ClassVar[str | None] = None
    plural: ClassVar[str | None] = None
    title_label: ClassVar[str] = "Title"
    roles: ClassVar[Sequence[Role]] = ()
    double_click: ClassVar[Literal["page", "open_file"]] = "page"
    card_lines: ClassVar[Sequence[str]] = ()
    contents_sort: ClassVar[Sequence["SortBy"]] = ()
    table_name: ClassVar[str | None] = None
    type_id: ClassVar[str | None] = None
    made_by_hand: ClassVar[bool] = False
    full_text: ClassVar[bool] = False
    description_field: ClassVar[str | None] = None
    folders_for: ClassVar[Sequence[type["Entity"]]] = ()


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
    ordered: bool = False
    """Each ``b``'s ``a`` items are in an order (a paper's authors): see :func:`related`."""
    symmetric: bool = False
    """A relationship of a type with itself that has no direction ("related"): see
    :func:`related`."""


def related(
    name: str,
    a: type[Entity],
    b: type[Entity],
    *,
    label: str | None = None,
    reverse_label: str | None = None,
    many: bool = True,
    ordered: bool = False,
    symmetric: bool = False,
) -> Relationship:
    """Declare a relationship between ``a`` and ``b`` items, such as
    ``related("cast", Actor, Movie, label="Filmography", reverse_label="Cast")``.

    ``label`` titles the section on an ``a`` item's page (an actor's movies: Filmography),
    and ``reverse_label`` the one on a ``b`` item's page (a movie's actors: Cast). The core
    builds a link table ``<theme id>_<name>``.

    ``ordered=True`` keeps each ``b``'s ``a`` items in an order, such as a paper's authors
    (API version 4): :meth:`IngestContext.relate` takes their ``position``,
    :meth:`IngestContext.related` gives a ``b``'s ``a`` items in that order, and a ``b``'s
    page lists them in it. The user can reorder them by hand; scans then leave that ``b``'s
    order alone. It needs ``many=True``.

    A relationship of a type with itself (``related("cites", Paper, Paper, label="Cites",
    reverse_label="Cited by")``) has a direction: an item's page shows both sides as
    separate sections, its ``a`` side under ``label`` and its ``b`` side under
    ``reverse_label`` (API version 4). ``symmetric=True`` (which needs ``a`` and ``b`` to be
    the same type) has none: one section lists the items related either way ("Related").
    """
    if not name.isidentifier():
        raise ThemeDeclarationError(f"relationship name {name!r} must be an identifier")
    if ordered and not many:
        raise ThemeDeclarationError(
            f"relationship {name!r}: ordered=True needs many=True (one item has no order)"
        )
    if symmetric and a is not b:
        raise ThemeDeclarationError(
            f"relationship {name!r}: symmetric=True relates a type with itself"
        )
    return Relationship(name, a, b, label, reverse_label, many, ordered, symmetric)


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
    aggregate_up: bool = False
    """A container matches when anything inside it matches (albums with a song tagged
    *Live*); the filter bar's "By contents" toggle starts on."""
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
    """Mark a :class:`Theme` method as an action: a command in the right-click menus of the
    items it applies to, and a button on their pages (DESIGN.md §9 "Actions").

    ``applies_to`` lists entity types, or role names (the action then applies to types that
    declare that role). The method is called as ``method(entities, ctx)``: ``entities`` is
    the list of :class:`EntityRef` it applies to (the selected ones, or the page's item),
    and ``ctx`` an :class:`ActionContext`. It runs in the DB writer, in one transaction: an
    exception undoes everything it wrote, and what it changed is one Edit → Undo step. Keep
    it quick (it holds up other writes), and never modify user files: write only to
    :meth:`ActionContext.temp_path`.
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
    counts_in: str | None = None
    """The folder the file counts as in when it is inside a see-through folder (#459): the
    nearest folder above that isn't one, root-relative (``""``, the top). ``None``: its own.
    Use :attr:`folder` rather than this."""
    see_through: frozenset[str] = frozenset()
    """The keep's see-through folder names, case-folded (``[folders] see_through``): for
    looking around on disk with :func:`files_in`."""

    @property
    def folder(self) -> str:
        """The root-relative folder the file counts as in: its own, or, when it is inside a
        see-through folder (``Heat (1995)/! Posters/poster.jpg``), the nearest folder above
        that isn't one (``Heat (1995)``). Judge what a file is by this folder."""
        return posixpath.dirname(self.relpath) if self.counts_in is None else self.counts_in

    @property
    def folder_path(self) -> str:
        """This machine's path to :attr:`folder`."""
        path = os.path.dirname(self.path)
        if self.counts_in is not None:
            real = posixpath.dirname(self.relpath)
            for _ in range(_depth(real) - _depth(self.counts_in)):
                path = os.path.dirname(path)
        return path


def _depth(folder: str) -> int:
    return folder.count("/") + 1 if folder else 0


def files_in(path: str, see_through: Iterable[str] = ()) -> list[str]:
    """The names of the files directly in the folder at ``path``, and in its see-through
    folders at any depth (``see_through``: :attr:`ResourceInfo.see_through`), sorted; none
    when it can't be listed. What a file is judged beside (#459)."""
    names = frozenset(n.casefold() for n in see_through)
    found: list[str] = []
    folders = [(path, False)]  # (folder, inside a see-through one)
    while folders:
        folder, inside = folders.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_file():
                        found.append(entry.name)
                    elif entry.is_dir() and (inside or entry.name.casefold() in names):
                        folders.append((entry.path, True))
        except OSError:
            continue
    return sorted(found)


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
    ) -> None:
        """Link a resource to an entity in a role; a one-file role's previous resource is
        replaced, except a file the user linked by hand, which stays (this link is skipped)."""
        ...

    def unlink(self, entity: EntityRef, resource: ResourceInfo | int, role: str) -> None:
        """Remove a link the theme made (links the user made by hand stay)."""
        ...

    def contain(self, parent: EntityRef, child: EntityRef) -> None: ...

    def uncontain(self, parent: EntityRef, child: EntityRef) -> None: ...

    def relate(self, name: str, a: EntityRef, b: EntityRef, position: int | None = None) -> None:
        """Relate ``a`` and ``b``. In an ordered relationship, ``position`` places ``a``
        among ``b``'s items (0 first; numbers need not be consecutive); without it, a new
        ``a`` goes last and an existing one keeps its place. After the user reorders
        ``b``'s items by hand, positions are ignored for it and new ones go last.
        ``position`` on an unordered relationship is an error. ``position`` needs
        ``api_version = 4``."""
        ...

    def unrelate(self, name: str, a: EntityRef, b: EntityRef) -> None: ...

    def related(self, name: str, entity: EntityRef) -> list[EntityRef]:
        """The entities related to ``entity`` through relationship ``name``, from either
        side (a movie's cast, an actor's movies), in id order; a ``b``'s items of an ordered
        relationship in their order (a paper's authors)."""
        ...

    def update(self, entity: EntityRef, *, title: str | None = None, **fields: Any) -> None:
        """Set extracted values on a known entity; user-edited values are left alone."""
        ...

    def entities_of(self, resource: ResourceInfo | int, role: str | None = None) -> list[EntityRef]:
        """Entities linked to a resource (in ``role``, if given). After a file is moved, its
        links follow it, so this is how file-based themes find "the entity for this file".
        Links the user made by hand aren't returned: that file isn't the theme's to read into
        the user's item."""
        ...

    def find(self, type: type[Entity], **equals: Any) -> list[EntityRef]: ...

    def get(self, entity: EntityRef) -> Record: ...

    def contents(self, entity: EntityRef) -> list[EntityRef]:
        """The entities ``entity`` directly contains, including this batch's changes."""
        ...

    def linked(self, entity: EntityRef, role: str | None = None) -> list[int]:
        """Ids of the resources linked to ``entity`` (in ``role``, if given)."""
        ...

    def delete(self, entity: EntityRef) -> None:
        """Delete an entity (for example an artist left empty). Its links, containment, and
        tags go with it; the resources stay. Use it only for entities the theme made."""
        ...

    def option(self, name: str) -> Any:
        """The value of a theme option (:func:`option`) for the root being scanned: the
        root's override, else the keep's setting, else the default."""
        ...

    def note(self, entity: EntityRef, folder: ResourceInfo | int) -> None:
        """The note inside ``folder`` describes ``entity`` (DESIGN.md §9 *Notes*): the
        Markdown file named after the folder, else ``index.md``, else ``README.md``. The core
        reads it after this scan's files, and again when it changes: its keys fill fields
        no file gave, the rest become Extra fields, its tags are file keywords, and its body
        is shown on the item's page. For an item whose primary file isn't a folder (a movie
        and its folder); an item whose primary file is a folder takes that folder's note
        without asking. ``folder`` must be a folder resource. Needs ``api_version = 8``."""
        ...

    def extra(self, entity: EntityRef, values: Mapping[str, Any]) -> None:
        """Extra fields from the file being read (#410): ``values`` are its keys the theme
        doesn't read into fields, such as :func:`read_front_matter`'s ``other``. The core
        treats them as a note's keys (DESIGN.md §9 *Notes*): each becomes an Extra field
        named as written, its value as text (lists joined with ``, ``, dates as
        ``YYYY-MM-DD``; tables of keys are left out), except ``tagalot-…`` keys, ``title``,
        ``tags``, ``keywords``, ``aliases``, and the keys the user ignores. They are marked
        as from the file and replace what the item's file gave before, so call it with
        every key each time the file is read (``{}`` when it gives none). A key the user
        removed or edited is left alone; for a key the item's note also gives, the file
        wins. Needs ``api_version = 11``."""
        ...

    def warn(self, resource: ResourceInfo | None, message: str) -> None:
        """Report a problem with a file to the activity panel."""
        ...

    def online_picture(self, entity: EntityRef, url: str) -> None:
        """In :meth:`Theme.online_details` only: ``url`` is a picture of ``entity`` (a
        book's cover, a person's photo), on one of the theme's :attr:`Theme.online_sources`.
        The core fetches it after the details, keeps it in the keep, and shows it only when
        the item has no picture of its own (DESIGN.md §10). Naming another address replaces
        the item's picture; an address already fetched isn't fetched again until the user
        asks (**Look up online**). A service without the picture should answer 404. Needs
        ``api_version = 21``."""
        ...

    def resource_at(
        self, beside: ResourceInfo, relpath: str, assets: str | None = None
    ) -> ResourceInfo | None:
        """The file or folder at ``relpath`` (POSIX, relative to the root) in the same root
        as ``beside``: a cover picture a Markdown file names, say, to :meth:`link` it. It
        must be among the files the theme accepts (:attr:`Theme.extensions`) and not known
        to be missing; else ``None``. Case is ignored when no file matches exactly. Needs
        ``api_version = 3``.

        With ``assets`` (DESIGN.md §9 *Markdown*, #418), a file not at ``relpath`` is also
        looked for under the assets folders, as ``beside`` named it (``relpath`` relative
        to ``beside``'s folder, so not one written with a leading ``/`` or ``..``): first
        ``assets`` itself, the item's own folder as its front matter (or the user) gave it,
        relative to ``beside``'s folder or to the root with a leading ``/`` (``""`` for
        none); then the folder's and the keep's default assets folders. Needs
        ``api_version = 9``."""
        ...

    def not_found(
        self,
        entity: EntityRef,
        beside: ResourceInfo,
        relpath: str | None,
        *,
        role: str,
        written: str,
        assets: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Say that a file ``beside`` names for ``entity``'s ``role`` wasn't found, such as a
        cover picture front matter names: Triage lists it with the paths looked in, so the
        user can fix the file or the assets folder setting (#426). Call it when
        :meth:`resource_at` gave ``None``, with the same ``relpath`` and ``assets``: Tagalot
        works out the paths it tried. ``written`` is what the file says, as written. When
        nothing could be looked for (a web address, a path outside the watched folder),
        pass ``relpath=None`` and say why in ``reason``.

        What a file reported is replaced each time that file is read, so a theme only
        reports what is still not found; it stops being listed once the item has a file
        in ``role`` (a picture scanned later), and goes with the item. Needs
        ``api_version = 13``."""
        ...

    def prepared(self, resource: ResourceInfo | int) -> Any:
        """What :meth:`Theme.prepare` returned for this resource, or ``None`` (it returned
        nothing for it, or there is no prepare step, as in ``migrate`` and actions)."""
        ...

    def keywords(
        self, entity: EntityRef, resource: ResourceInfo | int, keywords: Iterable[str]
    ) -> None:
        """What one file says the item is about: front matter's ``tags``, an EPUB's subjects,
        a comic's genres (DESIGN.md §7 "File keywords"). Replaces what that file said before;
        an empty list clears it. Keywords become tags only by matching tags the user
        defined (a full path such as ``Genre/Fantasy``, an alias, or a name), never by making
        new ones; unmatched ones are listed for the user to map."""
        ...


class ActionContext(IngestContext, Protocol):
    """What an action can do: everything :class:`IngestContext` can (its writes become one
    undo step), plus look up files and produce output. Output (opening, revealing,
    messages) happens after the action's changes are saved, in the order asked."""

    def contents(self, entity: EntityRef) -> list[EntityRef]:
        """The entities ``entity`` directly contains, in its type's ``contents_sort`` order
        (an album's songs by disc and track)."""
        ...

    def resources(self, entity: EntityRef, role: str | None = None) -> list[ResourceInfo]:
        """Files linked to ``entity`` (in ``role``, if given), in their sort order, with this
        computer's paths. Files that are missing, offline, or on a root with no path here
        are left out."""
        ...

    def temp_path(self, name: str) -> str:
        """A path named ``name`` in a folder of Tagalot's own, to write a file to (a
        playlist, say). The folder is deleted when the keep is closed."""
        ...

    def open(self, path: str) -> None:
        """Open a file with this computer's program for it (the user's override for its
        extension, else the default), once the action is done."""
        ...

    def reveal(self, path: str) -> None:
        """Show a file in the file manager, once the action is done."""
        ...

    def message(self, text: str) -> None:
        """Say something in the status bar when the action is done."""
        ...

    def copy_text(self, text: str) -> None:
        """Put ``text`` on the clipboard when the action is done (a citation). API
        version 4."""
        ...

    def open_url(self, url: str) -> None:
        """Open a web address (``http`` or ``https`` only; anything else is an error) in the
        browser when the action is done. Tagalot itself never fetches it. API version 4."""
        ...

    def save_text(self, name: str, text: str) -> None:
        """Offer to save ``text`` as a file (an export) when the action is done: the user
        picks where, starting from ``name`` (a file name, no folders). A place inside a
        watched folder is allowed after a warning; the file is then skipped by scans (so the
        keep doesn't read its own export back), and an existing file there is never
        replaced. API version 4."""
        ...


def kind_of(resource: ResourceInfo) -> Kind | None:
    """What kind of resource this is: ``Kind.DIR`` for folders, else by file extension;
    ``None`` for files of no known kind."""
    if resource.kind == "dir":
        return Kind.DIR
    return next((k for k, exts in KIND_EXTENSIONS.items() if resource.ext in exts), None)


# --- Schema migrations ---


class SchemaOps(Protocol):
    """Schema changes a theme asks the core to make in :meth:`Theme.migrate_schema`
    (DESIGN.md §9 "Theme schema versions"). They run in order, in the upgrade's
    transaction: if one fails, nothing changes.

    Each skips what the keep doesn't have (a field it never had, a type whose table isn't
    there yet), so a keep upgraded from an older version still works. Fields are named as
    in the entity class; the entity is one of the theme's current :attr:`Theme.entities`.
    """

    def rename_field(self, entity: type[Entity], old: str, new: str) -> None:
        """Rename a field, keeping its values, where they came from (so a user's edits stay
        protected), and saved searches using it. ``new`` is declared on ``entity``; ``old``
        no longer is. To change its type as well, call :meth:`change_type` after."""
        ...

    def change_type(
        self, entity: type[Entity], name: str, convert: Callable[[Any], Any] | None = None
    ) -> None:
        """Convert a field's stored values to the type it is declared with now.

        Without ``convert``, the core converts what clearly converts: numbers to and from
        text (``"120"`` to 120, 3.0 to 3), ``"true"``/``"no"``/1/0 to booleans, ISO text
        to dates and times, and anything to text; blank text becomes ``None`` (or the
        field's default, if it can't be ``None``). A value that doesn't convert fails the
        whole upgrade, naming the item and the value.

        ``convert``, if given, gets each stored value as SQLite holds it (``str``, ``int``,
        ``float``, or ``None``; dates and times as ISO text, booleans as 0 or 1) and returns
        the new value; raising ``ValueError`` fails the upgrade.
        """
        ...

    def drop_field(self, entity: type[Entity], name: str) -> None:
        """Remove a field the theme no longer declares, and its values, for good (the
        keep is backed up before an upgrade). Without this, a removed field's column is
        simply left in place, unused."""
        ...


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


class TitleCard(ThumbnailProvider):
    """A card with the item's title, for an item without a picture; ends the chain, like
    :class:`Icon`. The title is wrapped and as large as fits, on a soft color picked from it,
    so items without pictures still look different from each other. ``subtitle`` names a
    field shown under the title (a book's ``"authors"``); ``aspect`` is the card's width for
    each unit of height (``2 / 3`` like a book cover, ``1`` square). An item with no title
    goes on to the next provider. Put it last, so a real picture always wins:
    ``[RoleImage("cover"), ImageFile(), TitleCard("authors")]``."""

    id = "title_card"

    def __init__(self, subtitle: str | None = None, aspect: float = 2 / 3) -> None:
        if not 0.25 <= aspect <= 4:
            raise ValueError(f"a title card's aspect must be from 0.25 to 4, not {aspect}")
        self.subtitle = subtitle
        self.aspect = aspect

    def __repr__(self) -> str:
        args = [repr(self.subtitle)] if self.subtitle else []
        if self.aspect != 2 / 3:
            args.append(f"aspect={self.aspect!r}")
        return f"TitleCard({', '.join(args)})"


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


# --- Online details (DESIGN.md §9 "Online details") ---

_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")


@dataclass(frozen=True)
class OnlineSource:
    """An online service a theme looks items' details up in (API version 5), such as
    ``OnlineSource("Crossref", "api.crossref.org", "DOIs")``.

    ``name`` is how people know the service, ``host`` the only host Tagalot will fetch
    from for it, and ``sends`` what it is sent, as a plural ("DOIs", "ISBNs"): the keep asks
    the user once whether to look things up, naming each service and what it is sent.
    ``interval`` is the fewest seconds between two requests to the host (arXiv asks for 3).
    """

    name: str
    host: str
    sends: str
    interval: float = 1.0

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.sends.strip():
            raise ThemeDeclarationError("an OnlineSource needs a name and what it is sent")
        if not _HOST.fullmatch(self.host):
            raise ThemeDeclarationError(
                f"OnlineSource {self.name!r}: {self.host!r} must be a lowercase host name, "
                "such as 'api.crossref.org' (no https://, path, or port)"
            )
        if self.interval < 0:
            raise ThemeDeclarationError(f"OnlineSource {self.name!r}: interval can't be negative")


@dataclass(frozen=True)
class OnlineResponse:
    """What a service answered for one address (:meth:`Theme.online_details`)."""

    url: str
    status: int
    """The HTTP status: 200 for an answer; 404 when the service doesn't know the
    identifier, and so on. Failures to reach a service never get here."""
    text: str
    """The body, decoded as UTF-8."""

    @property
    def ok(self) -> bool:
        """The service answered with what was asked for (status 200 to 299)."""
        return 200 <= self.status < 300

    def json(self) -> Any:
        """The body as JSON; ``ValueError`` if it isn't."""
        return json.loads(self.text)


# --- The theme ---

DirRule = bool | Callable[[str], bool]


# --- Dashboard cards (DESIGN.md §12 "Dashboard") ---

STAT_KINDS = ("sum", "avg", "min", "max", "count")
"""What :func:`stat` can compute."""


@dataclass(frozen=True)
class StatCard:
    """A number computed over a field of one type; see :func:`stat`."""

    title: str
    type: type["Entity"]
    field: str
    how: str
    description: str = ""


@dataclass(frozen=True)
class TopValuesCard:
    """A field's most common values; see :func:`top_values`."""

    title: str
    type: type["Entity"]
    field: str
    limit: int = 5
    description: str = ""


DashboardCard = StatCard | TopValuesCard


def stat(
    title: str, type: type["Entity"], field: str, how: str = "sum", *, description: str = ""
) -> StatCard:
    """A dashboard card with one number over a field of ``type``'s items: ``how`` is
    ``"sum"``, ``"avg"``, ``"min"``, ``"max"``, or ``"count"`` (items with a value). It is
    shown in the field's display format: ``stat("Total running time", Song, "duration")``
    reads "1:02:33"."""
    if how not in STAT_KINDS:
        raise ThemeDeclarationError(f"stat {title!r}: how={how!r} is not one of {STAT_KINDS}")
    return StatCard(title, type, field, how, description)


def top_values(
    title: str, type: type["Entity"], field: str, *, limit: int = 5, description: str = ""
) -> TopValuesCard:
    """A dashboard card listing the most common values of a field of ``type``'s items, with
    how many have each; a value opens a search of those items:
    ``top_values("Top genres", Album, "genre")``."""
    if limit < 1:
        raise ThemeDeclarationError(f"top_values {title!r}: limit must be at least 1")
    return TopValuesCard(title, type, field, limit, description)


@dataclass(frozen=True)
class CardSpec:
    title: str
    description: str = ""


CardRows = Sequence[tuple[str, str]] | str
"""What a :func:`dashboard_card` method returns: ``(label, value)`` rows, or a text."""


class DashboardContext(Protocol):
    """What a :func:`dashboard_card` method can read (read-only; it runs in a worker
    whenever the dashboard is read, so keep it quick)."""

    def count(self, type: type["Entity"], **equals: Any) -> int:
        """How many items of ``type`` have these field values (all of them, without any)."""
        ...

    def find(self, type: type["Entity"], **equals: Any) -> list[EntityRef]:
        """The items of ``type`` with these field values."""
        ...

    def get(self, entity: EntityRef) -> Record:
        """An item's title, fields, and extra fields."""
        ...

    def stat(self, type: type["Entity"], field: str, how: str = "sum") -> float | None:
        """What :func:`stat` computes, as a number (``None`` with no values)."""
        ...


def dashboard_card(title: str, *, description: str = "") -> Callable[[_F], _F]:
    """Mark a :class:`Theme` method as a dashboard card computed by the theme, for what
    :func:`stat` and :func:`top_values` can't express. It is called as
    ``method(ctx)`` with a :class:`DashboardContext` and returns :data:`CardRows`. If it
    raises, the card shows the error and the rest of the dashboard is unaffected."""
    spec = CardSpec(title, description)

    def mark(method: _F) -> _F:
        method.__tagalot_card__ = spec  # type: ignore[attr-defined]
        return method

    return mark


class Theme:
    """Base for themes. A theme module defines exactly one subclass.

    Class attributes: ``id`` (lowercase identifier), ``name``, ``version`` (the theme's
    schema version; a new version reads every file again once), ``api_version``,
    ``extensions`` (accepted file extensions, lowercase, with the dot; empty = all),
    ``dirs`` (whether folders become resources: ``bool`` or a predicate on the relative
    path), ``entities``, ``containment``, ``relationships``, ``views``,
    ``near_duplicate_threshold`` (see :meth:`similarity`), ``dashboard`` (cards declared
    with :func:`stat` and :func:`top_values`, shown before cards from
    :func:`dashboard_card` methods), and thumbnail sizes: ``thumbnail_max`` (the
    resolution thumbnails are made and cached at; a keep can override it) and
    ``thumbnail_default`` (how big grid cards start; users zoom between small sizes and
    the max).
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
    options: ClassVar[Sequence[ThemeOption]] = ()
    dashboard: ClassVar[Sequence[DashboardCard]] = ()
    near_duplicate_threshold: ClassVar[float] = 0.9
    """How similar (0 to 1, from :meth:`similarity`) two items must be to be listed as
    near-duplicates."""
    thumbnail_max: ClassVar[int] = 256
    thumbnail_default: ClassVar[int] = 128
    read_last: ClassVar[Sequence[str] | frozenset[str] | set[str]] = frozenset()
    """Extensions (lowercase, with the dot) of files that describe other files, such as a
    library's bibliography export (API version 4): a scan reads them after its other
    files, and reads them again in any scan that read other files of their folder, so
    they meet files added later whichever came first. A tuple reads its earlier extensions
    first (bibliographies, then the notes that cite them); a set reads them by path."""
    write_back: ClassVar[Sequence[type[Entity]]] = ()
    """No longer needed (#501): **Write to file…** now updates the Markdown files of every
    type, and items' notes (DESIGN.md §4 *Writing back to files*): their tags, and the
    fields the user edited under the keys :meth:`front_matter` gives (else under the key the
    file uses for the field, or its name). Only files in folders the user marked writable are
    ever changed, only by that command, and only their front matter; the core does the
    writing. Kept so themes that set it still load (API version 3)."""

    online_sources: ClassVar[Sequence[OnlineSource]] = ()
    """The online services this theme looks items up in (DESIGN.md §9 *Online details*;
    API version 5). Only the core goes online, only with the user's consent for the keep,
    and only to these hosts: :meth:`online_requests` says what to fetch and
    :meth:`online_details` reads the answers."""

    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        """Read what :meth:`ingest` needs from the files, before it runs: image sizes, tags,
        font names. Returns ``{resource id: value}``; ``ingest`` gets each value with
        ``ctx.prepared(resource)``.

        It runs on scan worker threads, outside any database transaction, so slow reads (a
        network share) never hold up other writes, and the next batch is read while the
        last is ingested. It has no ``ctx`` and must not touch the keep. Several calls run at
        once, each with a few resources, so keep what it remembers local to the call (or
        guard it with a lock): not in ``self`` or module globals.
        Catch problems with single files and leave them out (or ``ctx.warn`` about them
        later); if ``prepare`` raises, the core retries the batch one resource at a time and
        a resource that still fails is reported and stays pending for the next scan. The
        default reads nothing.
        """
        return {}

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        """Turn new and changed resources into entities, links, containment, and fields.

        This runs inside a database write transaction: read files in :meth:`prepare`."""

    def migrate(self, from_version: int, ctx: IngestContext) -> None:
        """Upgrade this theme's *data* from ``from_version`` to :attr:`version`.

        Runs after the user confirms, with ``keep.db`` backed up, in one transaction. Additive
        schema changes (new entity types, new fields) are applied by the core before this is
        called, so a theme that only adds things needs no ``migrate`` at all; the default does
        nothing.
        """

    def migrate_schema(self, from_version: int, ops: SchemaOps) -> None:
        """Rename, retype, or drop fields when upgrading from ``from_version`` (e.g.
        ``if from_version < 3: ops.rename_field(Song, "tempo", "bpm")``).

        Runs first in the upgrade's transaction, before the core adds new tables and
        fields and before :meth:`migrate`; see :class:`SchemaOps`. A theme that defines it
        sets ``api_version = 2``, so an older Tagalot refuses the theme rather than skip
        its schema changes. The default does nothing.
        """

    def front_matter(
        self,
        entity_type: type[Entity],
        item: Record,
        edited: frozenset[str],
        current: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """The front-matter keys **Write to file…** sets in one item's own Markdown file:
        ``{key: value}``, a value of ``None`` removing the key.

        ``edited`` names the item's fields (and ``"title"``) the user edited in Tagalot;
        only those are written, so return keys for them alone (values that came from the
        file are already there). ``current`` is the file's front matter now, keys as
        written, so a key it already uses can be kept (``number`` rather than
        ``series_index``). Values may be text, numbers, booleans, dates, or lists of them.
        Tags are the core's: it writes them under ``tags`` (keeping the file's own words for
        tags its keywords gave). Without this method, each edited field goes under the key
        the file already uses for it (its name or label, as notes are read), else its name;
        notes are always written that way (#501).
        """
        return {}

    def online_requests(self, entity_type: type[Entity], item: Record) -> Iterable[str]:
        """The addresses to fetch to look up one item, built from its fields (a paper's
        DOI): ``https`` addresses on the hosts of :attr:`online_sources`, any others being
        refused. None (the default) when there is nothing to look up. It runs in a worker
        for many items at a time, so it must be quick and must not go online itself.
        API version 5."""
        return ()

    def online_details(
        self,
        entity: EntityRef,
        responses: Mapping[str, OnlineResponse],
        ctx: IngestContext,
    ) -> None:
        """Turn what the services answered for one item into its details, once every
        address :meth:`online_requests` gave has an answer: ``responses`` maps each to its
        :class:`OnlineResponse` (check :attr:`~OnlineResponse.ok`). Answers are kept in the
        keep, so this also runs again from them, without going online.

        It runs in the DB writer, like :meth:`ingest`: fields it sets are marked as
        looked up (provenance ``fetched``), and never replace what the user edited. Looked-up
        details should not replace what the item's files say either: fill in empty fields,
        or rank the sources as the research theme does. Name pictures of the items with
        :meth:`IngestContext.online_picture` (API version 21). API version 5."""

    def document_text(self, resource: ResourceInfo) -> Sequence[str] | None:
        """The text of a :attr:`Entity.full_text` type's file, as pages, for searching
        inside documents; ``None`` (the default) for the core's reading of it,
        :func:`read_document_text`. Override it for a format the core doesn't read, or to
        read one differently. It runs on a background reading thread, like
        :meth:`prepare`: read only this file, keep nothing in ``self``, and raise
        ``OSError`` or ``ValueError`` for a file that can't be read (it is reported, and
        not tried again until it changes). API version 6."""
        return None

    def blocking_keys(self, entity_type: type[Entity], record: Record) -> Iterable[str]:
        """Cheap keys for finding near-duplicates (DESIGN.md §13): only items of a type
        that share a key are compared with :meth:`similarity`, never all pairs. Build them
        from the record's fields (normalized artist and title; parts of an image hash).
        The default gives none, so the theme has no near-duplicates."""
        return ()

    def similarity(self, entity_type: type[Entity], a: Record, b: Record) -> float:
        """How alike two items of ``entity_type`` that share a blocking key are, from 0
        (not at all) to 1 (the same); pairs at or above :attr:`near_duplicate_threshold`
        are listed. Keep it quick: it runs for every pair sharing a key."""
        return 0.0

    def item_named(self, entity_type: type[Entity], name: str, ctx: "IngestContext") -> EntityRef:
        """The item of ``entity_type`` called ``name``, made if there is none: what the user
        typed into a field that mirrors a link (:func:`field`'s ``mirrors``, API version 15).
        Override it to find items the way your ``ingest`` makes them (with the same
        :meth:`IngestContext.upsert` keys), so an edit and a later scan agree on one item.
        The default: an item of that type with that title, else a new one keyed by its
        title (``<type>:<title, case-folded>``)."""
        found = ctx.find(entity_type, title=name)
        if found:
            return found[0]
        key = f"{entity_type.__name__.lower()}:{' '.join(name.split()).casefold()}"
        return ctx.upsert(entity_type, key, title=name)

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

    @classmethod
    def card_methods(cls) -> dict[str, CardSpec]:
        """Method name -> card, for methods marked with :func:`dashboard_card`, in
        declaration order."""
        found: dict[str, CardSpec] = {}
        for klass in reversed(cls.__mro__):
            for name, value in vars(klass).items():
                spec = getattr(value, "__tagalot_card__", None)
                if isinstance(spec, CardSpec):
                    found[name] = spec
        return found


# --- Reading files (DESIGN.md §9 books, "Reading files") ---

from tagalot.themes.bibliography import BIBLIOGRAPHY_EXTENSIONS, read_bibliography  # noqa: E402
from tagalot.themes.document_text import DOCUMENT_EXTENSIONS, read_document_text  # noqa: E402
from tagalot.themes.mobi import MOBI_EXTENSIONS, mobi_cover, read_mobi  # noqa: E402
from tagalot.themes.readers import (  # noqa: E402
    PDFIUM,
    epub_cover,
    find_identifiers,
    office_cover,
    oldest,
    pdf_cover,
    pdf_text,
    read_archive,
    read_comic_info,
    read_epub,
    read_front_matter,
    read_link_file,
    read_office_info,
    read_pdf_info,
    read_picture,
    read_rating,
    split_keywords,
    split_people,
)

__all__ = [
    "API_VERSION",
    "BIBLIOGRAPHY_EXTENSIONS",
    "DISPLAY_FORMATS",
    "DOCUMENT_EXTENSIONS",
    "FIELD_TYPES",
    "FOLDER_IMAGE_EXTENSIONS",
    "FOLDER_IMAGE_NAMES",
    "KIND_EXTENSIONS",
    "MOBI_EXTENSIONS",
    "OPTION_TYPES",
    "PDFIUM",
    "RATING_SCALES",
    "SEARCH_KINDS",
    "ActionContext",
    "ActionSpec",
    "ArchiveFirstImage",
    "CardRows",
    "Containment",
    "DashboardCard",
    "DashboardContext",
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
    "OnlineResponse",
    "OnlineSource",
    "ParentThumbnail",
    "Record",
    "Relationship",
    "ResourceInfo",
    "Role",
    "RoleImage",
    "SchemaOps",
    "SearchView",
    "Section",
    "SortBy",
    "StatCard",
    "Theme",
    "ThemeDeclarationError",
    "ThemeOption",
    "ThumbnailContext",
    "ThumbnailProvider",
    "TitleCard",
    "TopValuesCard",
    "View",
    "action",
    "contains",
    "dashboard_card",
    "default_thumbnail_chain",
    "entity_fields",
    "entity_label",
    "entity_plural",
    "epub_cover",
    "field",
    "files_in",
    "find_identifiers",
    "kind_of",
    "mobi_cover",
    "office_cover",
    "oldest",
    "option",
    "pdf_cover",
    "pdf_text",
    "plural_of",
    "read_archive",
    "read_bibliography",
    "read_comic_info",
    "read_document_text",
    "read_epub",
    "read_front_matter",
    "read_link_file",
    "read_mobi",
    "read_office_info",
    "read_pdf_info",
    "read_picture",
    "read_rating",
    "related",
    "role",
    "split_keywords",
    "split_people",
    "stat",
    "top_values",
]
