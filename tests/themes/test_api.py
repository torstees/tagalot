"""Tests for the public theme API (DESIGN.md §9)."""

import ast
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

import tagalot.themes.api as api
from tagalot.themes.api import (
    API_VERSION,
    DetailView,
    Entity,
    EntityRef,
    FieldInfo,
    IngestContext,
    Kind,
    Record,
    ResourceInfo,
    SearchView,
    Section,
    SortBy,
    Theme,
    ThemeDeclarationError,
    action,
    contains,
    entity_fields,
    entity_label,
    entity_plural,
    field,
    plural_of,
    related,
    role,
)

# --- The DESIGN.md §9 example, as written there ---


class Actor(Entity):
    title_label = "Name"
    born: date | None = field("Date of birth", search="range")
    country: str | None = field("Country", search="choice")
    roles = [role("photo", kinds={"image"}, many=False, thumbnail=True)]


class Collection(Entity):
    roles = [role("folder", kinds={"dir"}, primary=True)]


class Movie(Entity):
    year: int | None = field("Year", card=True, search="range")
    roles = [
        role("video", kinds={"video"}, many=True, primary=True),
        role("poster", kinds={"image"}, thumbnail=True),
        role("screenshot", kinds={"image"}, many=True),
    ]


class MoviesTheme(Theme):
    id, name, version = "movies", "Movies", 1
    extensions = {".mkv", ".mp4", ".avi", ".jpg", ".png"}
    entities = [Actor, Collection, Movie]
    containment = [contains(Collection, Movie)]
    relationships = [related("cast", Actor, Movie, label="Cast", reverse_label="Filmography")]
    views = [
        SearchView("Movies", types=[Movie], inherit_tags=True, show_contained=False),
        SearchView("Actors", types=[Actor]),
        DetailView(
            Movie,
            sections=[
                Section.fields(),
                Section.role("poster"),
                Section.related("cast"),
                Section.gallery("screenshot"),
            ],
        ),
    ]

    @action("Play trailer", applies_to=[Movie])
    def play_trailer(self, entities: list[EntityRef], ctx: IngestContext) -> None:
        pass


def test_design_example_declares_cleanly() -> None:
    assert MoviesTheme.id == "movies"
    assert MoviesTheme.api_version == API_VERSION == 4
    assert [e.__name__ for e in MoviesTheme.entities] == ["Actor", "Collection", "Movie"]
    assert MoviesTheme.containment[0] == contains(Collection, Movie)
    assert MoviesTheme.relationships[0].name == "cast"


def test_fields_are_introspected_with_types() -> None:
    fields = {f.name: f for f in entity_fields(Actor)}
    assert list(fields) == ["born", "country"]
    assert (fields["born"].type, fields["born"].nullable) == (date, True)
    assert fields["born"].spec.label == "Date of birth"
    assert fields["born"].spec.search == "range"
    assert fields["country"].spec.search == "choice"
    assert entity_fields(Collection) == []


def test_field_spec_knows_its_name() -> None:
    assert vars(Movie)["year"].name == "year"


def test_all_field_types() -> None:
    class AllTypes(Entity):
        s: str = field("S")
        i: int = field("I")
        f: float = field("F")
        b: bool = field("B")
        d: date = field("D")
        dt: datetime = field("DT")
        maybe: str | None = field("Maybe")

    types = {f.name: (f.type, f.nullable) for f in entity_fields(AllTypes)}
    assert types == {
        "s": (str, False),
        "i": (int, False),
        "f": (float, False),
        "b": (bool, False),
        "d": (date, False),
        "dt": (datetime, False),
        "maybe": (str, True),
    }


def test_subclass_fields_extend_and_override() -> None:
    class Base(Entity):
        a: int = field("A")
        b: int = field("B")

    class Child(Base):
        b: str = field("B as text")  # type: ignore[assignment]
        c: int = field("C")

    fields = entity_fields(Child)
    assert [(f.name, f.type) for f in fields] == [("a", int), ("b", str), ("c", int)]


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        ("list[str]", "is not a field type"),
        ("str | int", "is not a field type"),
        ("dict[str, int] | None", "is not a field type"),
    ],
)
def test_unsupported_field_types(annotation: str, message: str) -> None:
    namespace: dict[str, object] = {}
    exec(
        f"from tagalot.themes.api import Entity, field\n"
        f"class Bad(Entity):\n    x: {annotation} = field('X')\n",
        namespace,
    )
    with pytest.raises(ThemeDeclarationError, match=message):
        entity_fields(namespace["Bad"])  # type: ignore[arg-type]


def test_field_without_annotation() -> None:
    class Bad(Entity):
        x = field("X")

    with pytest.raises(ThemeDeclarationError, match="needs a type annotation"):
        entity_fields(Bad)


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: field("Year", search="fuzzy"), "search='fuzzy'"),  # type: ignore[arg-type]
        (lambda: field("  "), "needs a label"),
        (lambda: role("poster", kinds={"picture"}), "kinds are image, audio"),
        (lambda: role("poster", kinds=[]), "at least one kind"),
        (lambda: related("the cast", Actor, Movie), "must be an identifier"),
    ],
)
def test_invalid_declarations_fail_immediately(make: object, message: str) -> None:
    with pytest.raises(ThemeDeclarationError, match=message):
        make()  # type: ignore[operator]


def test_roles() -> None:
    video, poster, _ = Movie.roles
    assert video.kinds == {Kind.VIDEO}
    assert (video.many, video.primary, video.thumbnail) == (True, True, False)
    assert poster.thumbnail
    assert role("art", kinds=["image", "any"]).kinds == {Kind.IMAGE, Kind.ANY}


def test_type_ids_and_table_names() -> None:
    assert MoviesTheme.type_id_of(Movie) == "movies.movie"
    assert MoviesTheme.table_name_of(Collection) == "movies_collection"

    class Episode(Entity):
        type_id = "movies.tv_episode"
        table_name = "movies_episodes"

    assert MoviesTheme.type_id_of(Episode) == "movies.tv_episode"
    assert MoviesTheme.table_name_of(Episode) == "movies_episodes"


def test_entity_defaults() -> None:
    assert (Movie.title_label, Movie.double_click) == ("Title", "page")
    assert Actor.title_label == "Name"


def test_views() -> None:
    movies, _actors, detail = MoviesTheme.views
    assert isinstance(movies, SearchView)
    assert (movies.inherit_tags, movies.layout, movies.default_sort) == (
        True,
        "grid",
        (SortBy("title"),),
    )
    assert isinstance(detail, DetailView)
    assert [s.kind for s in detail.sections] == ["fields", "role", "related", "gallery"]
    assert Section.contents().kind == "contents"

    def factory(context: object) -> object:
        return context

    assert Section.custom(factory).factory is factory


def test_actions() -> None:
    assert MoviesTheme.actions() == {
        "play_trailer": api.ActionSpec("Play trailer", (Movie,)),
    }

    class Sub(MoviesTheme):
        @action("Play album", applies_to=["folder"])
        def play(self, entities: list[EntityRef], ctx: IngestContext) -> None:
            pass

    assert set(Sub.actions()) == {"play_trailer", "play"}
    assert Sub.actions()["play"].applies_to == ("folder",)


def test_default_migrate_does_nothing() -> None:
    # Additive changes are handled by the core; data migrations are opt-in.
    assert MoviesTheme().migrate(0, None) is None  # type: ignore[arg-type,func-returns-value]


def test_values() -> None:
    ref = EntityRef(7, "movies.movie")
    record = Record(ref, "Alien", {"year": 1979}, {"notes": "director's cut"})
    info = ResourceInfo(3, "films", "Alien (1979)/alien.mkv", "file", ".mkv", 10, 20, "/x")
    assert record.fields["year"] == 1979
    assert info.relpath.endswith(".mkv")
    with pytest.raises(AttributeError):
        ref.id = 8  # type: ignore[misc]
    assert isinstance(entity_fields(Movie)[0], FieldInfo)


def _imports(source: str, top_level_only: bool = False) -> set[str]:
    tree = ast.parse(source)
    nodes = tree.body if top_level_only else list(ast.walk(tree))
    imported: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_api_imports_only_the_standard_library() -> None:
    # The core and the launcher import the API; it must not pull in SQLAlchemy, Qt, or
    # other tagalot modules (DESIGN.md §9 "Mapping and isolation"), except its file
    # readers (#296), which import only the standard library when loaded.
    imported = _imports(Path(api.__file__).read_text(encoding="utf-8"))
    others = {m for m in imported if m.split(".")[0] not in sys.stdlib_module_names}
    assert others == {"tagalot.themes.readers"}
    readers = Path(api.__file__).with_name("readers.py").read_text(encoding="utf-8")
    loaded = {m.split(".")[0] for m in _imports(readers, top_level_only=True)}
    assert loaded <= set(sys.stdlib_module_names), loaded - set(sys.stdlib_module_names)


@pytest.mark.parametrize(
    ("word", "plural"),
    [
        ("Album", "Albums"),
        ("Box", "Boxes"),
        ("Match", "Matches"),
        ("Brush", "Brushes"),
        ("Glass", "Glasses"),
        ("Category", "Categories"),
        ("Key", "Keys"),  # a vowel before the y
        ("TV Show", "TV Shows"),
        ("Y", "Ys"),
    ],
)
def test_plural_of(word: str, plural: str) -> None:
    assert plural_of(word) == plural


def test_entity_labels_and_plurals() -> None:
    class Series(Entity):
        plural = "Series"

    class Person(Entity):
        label = "Actor or actress"
        plural = "Cast"

    class AudioBook(Entity):
        label = "Audiobook"

    assert (entity_label(Movie), entity_plural(Movie)) == ("Movie", "Movies")
    assert (entity_label(Series), entity_plural(Series)) == ("Series", "Series")
    assert (entity_label(Person), entity_plural(Person)) == ("Actor or actress", "Cast")
    assert (entity_label(AudioBook), entity_plural(AudioBook)) == ("Audiobook", "Audiobooks")


# --- thumbnails ---


def _resource(relpath: str, kind: str = "file") -> api.ResourceInfo:
    ext = (
        ""
        if kind == "dir"
        else ("." + relpath.rpartition(".")[2].lower() if "." in relpath else "")
    )
    return api.ResourceInfo(1, "r", relpath, kind, ext, None, None, relpath)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("relpath", "kind", "expected"),
    [
        ("a/cover.jpg", "file", api.Kind.IMAGE),
        ("a/Cover.JPEG", "file", api.Kind.IMAGE),
        ("a/01.flac", "file", api.Kind.AUDIO),
        ("film.mkv", "file", api.Kind.VIDEO),
        ("font.otf", "file", api.Kind.FONT),
        ("pack.zip", "file", api.Kind.ARCHIVE),
        ("a", "dir", api.Kind.DIR),
        ("notes.txt", "file", None),
        ("README", "file", None),
    ],
)
def test_kind_of(relpath: str, kind: str, expected: api.Kind | None) -> None:
    assert api.kind_of(_resource(relpath, kind)) == expected


def test_no_extension_has_two_kinds() -> None:
    seen: set[str] = set()
    for exts in api.KIND_EXTENSIONS.values():
        assert not exts & seen
        seen |= exts


class _Poster(api.Entity):
    roles = [
        api.role("video", kinds={"video"}, primary=True),
        api.role("poster", kinds={"image"}, thumbnail=True),
        api.role("still", kinds={"image"}, many=True, thumbnail=True),
    ]


class _Picture(api.Entity):
    roles = [api.role("image", kinds={"image"}, primary=True, thumbnail=True)]


class _Anything(api.Entity):
    roles = [api.role("file", kinds={"any"}, primary=True)]


class _Person(api.Entity):
    pass


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        (_Poster, "[RoleImage('poster'), RoleImage('still'), ImageFile(), Icon('video')]"),
        (_Picture, "[RoleImage('image'), Icon('image')]"),
        (_Anything, "[ImageFile(), Icon('file')]"),
        (_Person, "[Icon('entity')]"),
    ],
)
def test_default_thumbnail_chains(entity: type[api.Entity], expected: str) -> None:
    assert repr(api.default_thumbnail_chain(entity)) == expected

    class Themed(api.Theme):
        id, name = "t", "T"
        entities = [entity]

    assert repr(list(Themed().thumbnail_chain(entity))) == expected
