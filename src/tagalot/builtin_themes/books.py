"""Books theme: books and comics, their writers and artists, series, universes, and
collections (DESIGN.md §9, books).

```
Universe ⊃ Series ⊃ Book, Comic     Author ↔ Book, Comic (writers, artists)
Universe ⊃ Book, Comic              Collection ⊃ Book, Comic
```

- **A work and its files:** a book (EPUB) or comic (CBZ, CBR, CB7) is keyed by its title and
  first writer (a comic by its series, volume, and number), so the same novel bought from two
  sites is one work whose files are its versions. A file already linked to a work updates it
  in place. What a file says replaces what it said before only while it is the work's one
  file; with several, each adds to the work (its writers, its series).
- **Reading files** (``prepare``, a scan worker): an EPUB's package metadata, a comic's
  ``ComicInfo.xml`` (the :mod:`tagalot.themes.api` readers), else the file name:
  ``Author - Title (Year)``, ``Series 03 - Title``, ``Series #012 (2020)``.
- **Sources:** where a file came from: the folder at the ``source_level`` option's depth
  (``1`` for ``Humble Bundle/…``), else a comic's web address's site, else an EPUB's
  publisher. A work's ``sources`` lists its files'.
- **Keywords:** an EPUB's subjects and a comic's genres and tags become tags only through
  file keywords (DESIGN.md §7).
- **Thumbnails:** a work shows its cover (an EPUB's named cover, a comic's first page); a
  series, universe, or collection one of its first works'.
"""

import difflib
import logging
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from tagalot.themes.api import (
    DetailView,
    Entity,
    EntityRef,
    Icon,
    ImageFile,
    IngestContext,
    Record,
    ResourceInfo,
    SearchView,
    Section,
    SortBy,
    Theme,
    ThumbnailContext,
    ThumbnailProvider,
    contains,
    field,
    option,
    read_comic_info,
    read_epub,
    related,
    role,
    top_values,
)

logger = logging.getLogger(__name__)

BOOK_EXTENSIONS = frozenset({".epub"})
COMIC_EXTENSIONS = frozenset({".cbz", ".cbr", ".cb7"})


class Author(Entity):
    """Someone who wrote or drew works: one per spelling the files give (merge them to
    join a writer's spellings)."""

    title_label = "Name"
    sort_name: str | None = field("Sort name", search="text")
    """``Pratchett, Terry``: how the Authors view sorts them."""


class Universe(Entity):
    """Series and works sharing a world (Discworld, Cosmere, Marvel)."""

    contents_sort = (SortBy("title"),)


class Series(Entity):
    """Works in reading order."""

    plural = "Series"
    universe: str | None = field("Universe", search="choice", editable=False)
    contents_sort = (SortBy("series_index"), SortBy("title"))
    card_lines = ("universe",)


class Collection(Entity):
    """An omnibus, anthology, or box set holding works."""

    contents_sort = (SortBy("series_index"), SortBy("title"))


class Book(Entity):
    """A book; its files (an EPUB from one store, another from a second) are its versions."""

    authors: str | None = field("Author", card=True, search="choice", editable=False)
    """Its writers' names, as its files give them (the credits list them as people)."""
    series: str | None = field("Series", card=True, search="choice", editable=False)
    series_index: float | None = field("Number in series", search="range")
    universe: str | None = field("Universe", search="choice", editable=False)
    collections: str | None = field("Collections", search="text", editable=False)
    year: int | None = field("Year", search="range")
    publisher: str | None = field("Publisher", search="choice")
    language: str | None = field("Language", search="choice")
    isbn: str | None = field("ISBN", search="text")
    description: str | None = field("Description", search="text")
    sources: str | None = field("Sources", search="text", editable=False)
    """Where its files came from (``Humble Bundle, Kobo``)."""
    link: str | None = field("Link", search="text")
    roles = [role("file", kinds={"any"}, many=True, primary=True, label="Files")]
    card_lines = ("authors", "series")


class Comic(Entity):
    """A comic issue or volume; its files are its versions."""

    series: str | None = field("Series", card=True, search="choice", editable=False)
    number: str | None = field("Number", card=True, search="text")
    """As written: ``12``, ``1.5``, ``Annual 1``."""
    volume: int | None = field("Volume", search="range")
    series_index: float | None = field("Reading order", search="range", detail=False)
    """The number as a number, for sorting a series (``Annual 1`` has none)."""
    universe: str | None = field("Universe", search="choice", editable=False)
    writers: str | None = field("Writer", card=True, search="choice", editable=False)
    artists: str | None = field("Artist", search="choice", editable=False)
    story_arc: str | None = field("Story arc", search="choice")
    year: int | None = field("Year", search="range")
    publisher: str | None = field("Publisher", search="choice")
    imprint: str | None = field("Imprint", search="choice")
    description: str | None = field("Summary", search="text")
    sources: str | None = field("Sources", search="text", editable=False)
    link: str | None = field("Link", search="text")
    roles = [role("file", kinds={"any"}, many=True, primary=True, label="Files")]
    card_lines = ("series", "number")


WORKS: tuple[type[Entity], ...] = (Book, Comic)
CREDITS: Mapping[type[Entity], tuple[str, str]] = {
    Book: ("writers", "artists"),
    Comic: ("comic_writers", "comic_artists"),
}
"""Each work type's relationships to its writers and to its artists: a relationship joins
two types, so books and comics each have their own."""


class ContentsThumbnail(ThumbnailProvider):
    """A series, universe, or collection shows one of its first works' covers."""

    id = "books.contents"
    tried = 5

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        for child in ctx.children(entity)[: self.tried]:
            found = ctx.thumbnail_of(child)
            if found is not None:
                yield found


class BooksTheme(Theme):
    """Books and comics, with their people, series, universes, and collections."""

    id, name, version = "books", "Books", 1
    extensions = BOOK_EXTENSIONS | COMIC_EXTENSIONS
    entities = [Author, Universe, Series, Collection, Book, Comic]
    containment = [
        contains(Universe, Series),
        contains(Universe, Book),
        contains(Universe, Comic),
        contains(Series, Book),
        contains(Series, Comic),
        contains(Collection, Book),
        contains(Collection, Comic),
    ]
    relationships = [
        related("writers", Author, Book, label="Books", reverse_label="Written by"),
        related("artists", Author, Book, label="Illustrated", reverse_label="Art by"),
        related("comic_writers", Author, Comic, label="Comics", reverse_label="Written by"),
        related("comic_artists", Author, Comic, label="Comic art", reverse_label="Art by"),
    ]
    options = [
        option(
            "source_level",
            0,
            label="Source folder level",
            description="Which folder names the site a file came from: 1 for "
            "'Humble Bundle/…', 2 for 'Ebooks/Kobo/…'; 0 for none.",
        )
    ]
    near_duplicate_threshold = 0.85
    dashboard = [
        top_values("Top publishers", Book, "publisher"),
        top_values("Top languages", Book, "language"),
        top_values("Top comic publishers", Comic, "publisher"),
    ]
    views = [
        # Tags on a series or universe count for its works: tag Discworld "Fantasy" and
        # Books with Fantasy lists its novels.
        SearchView(
            "Books",
            [Book],
            inherit_tags=True,
            default_sort=[
                SortBy("authors"),
                SortBy("series"),
                SortBy("series_index"),
                SortBy("title"),
            ],
        ),
        SearchView(
            "Comics",
            [Comic],
            inherit_tags=True,
            default_sort=[
                SortBy("series"),
                SortBy("volume"),
                SortBy("series_index"),
                SortBy("title"),
            ],
        ),
        SearchView("Authors", [Author], default_sort=[SortBy("sort_name"), SortBy("title")]),
        SearchView("Series", [Series], inherit_tags=True),
        SearchView("Universes", [Universe]),
        SearchView("Collections", [Collection], inherit_tags=True),
        DetailView(
            Book,
            [
                Section.fields(),
                Section.role("file"),
                Section.related("writers"),
                Section.related("artists"),
            ],
        ),
        DetailView(
            Comic,
            [
                Section.fields(),
                Section.role("file"),
                Section.related("comic_writers"),
                Section.related("comic_artists"),
            ],
        ),
        DetailView(
            Author,
            [
                Section.fields(),
                Section.related("writers"),
                Section.related("comic_writers"),
                Section.related("artists"),
                Section.related("comic_artists"),
            ],
        ),
    ]

    def thumbnail_chain(self, entity_type: type[Entity]) -> Sequence[ThumbnailProvider]:
        if entity_type is Book:
            return [ImageFile(), Icon("file")]
        if entity_type is Comic:
            return [ImageFile(), Icon("archive")]
        if entity_type in (Series, Universe, Collection):
            return [ContentsThumbnail(), Icon("entity")]
        return super().thumbnail_chain(entity_type)

    # --- near-duplicates ---

    def blocking_keys(self, entity_type: type[Entity], record: Record) -> Iterable[str]:
        """Works by the same first writer."""
        if entity_type not in WORKS:
            return ()
        names = record.fields.get("authors" if entity_type is Book else "writers") or ""
        first = normalize(names.split(",")[0])
        return [first] if first else []

    def similarity(self, entity_type: type[Entity], a: Record, b: Record) -> float:
        """How alike the titles are (ignoring case and punctuation); different numbers in
        a series are different works however alike their titles."""
        numbers = a.fields.get("series_index"), b.fields.get("series_index")
        if None not in numbers and numbers[0] != numbers[1]:
            return 0.0
        return difflib.SequenceMatcher(None, normalize(a.title), normalize(b.title)).ratio()

    # --- reading files (scan worker) ---

    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        found: dict[int, Any] = {}
        for resource in batch:
            if resource.kind != "file" or not resource.path:
                continue
            try:
                if resource.ext in BOOK_EXTENSIONS:
                    found[resource.id] = book_work(resource.relpath, read_epub(resource.path))
                elif resource.ext in COMIC_EXTENSIONS:
                    info = read_comic_info(resource.path)
                    found[resource.id] = comic_work(resource.relpath, info)
            except Exception as e:  # a bad file is still a work, named from its file
                fallback = book_work if resource.ext in BOOK_EXTENSIONS else comic_work
                found[resource.id] = fallback(resource.relpath, None)
                found[resource.id]["error"] = f"{type(e).__name__}: {e}"
        return found

    # --- ingest (DB writer) ---

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            work = ctx.prepared(resource)
            if work:
                self._ingest_work(resource, dict(work), ctx)

    def _ingest_work(
        self, resource: ResourceInfo, work: dict[str, Any], ctx: IngestContext
    ) -> None:
        error = work.pop("error", None)
        if error:
            ctx.warn(resource, f"Couldn't read its details: {error}")
        kind: type[Entity] = Book if work.pop("type") == "book" else Comic
        writers: list[str] = work.pop("writers")
        artists: list[str] = work.pop("artists")
        keywords: list[str] = work.pop("keywords")
        collections: list[str] = work.pop("collections")
        source = source_of(resource.relpath, ctx.option("source_level"), work.pop("source"))
        title = work.pop("title")

        existing = ctx.entities_of(resource, "file")
        if existing:
            entity = existing[0]
        else:
            entity = ctx.upsert(kind, work_key(kind, title, writers, work, resource.relpath))
            ctx.link(entity, resource, "file")
        alone = not [r for r in ctx.linked(entity, "file") if r != resource.id]
        before = ctx.get(entity).fields

        values = dict(work)
        if kind is Book:
            values["collections"] = ", ".join(collections) or None
        names = ", ".join(writers) or None
        if kind is Book:
            values["authors"] = names
        else:
            values["writers"] = names
            values["artists"] = ", ".join(artists) or None
        values["sources"] = (
            ", ".join(merged([] if alone else split(before.get("sources")), source)) or None
        )
        if not alone:  # another file may have said what this one doesn't
            values = {k: v for k, v in values.items() if v is not None and v != ""}
            for name in ("authors", "writers", "artists", "collections"):
                if name in values:
                    values[name] = ", ".join(merged(split(before.get(name)), *split(values[name])))
        ctx.update(entity, title=title if alone else None, **values)
        ctx.keywords(entity, resource, keywords)

        writes, draws = CREDITS[kind]
        set_people(entity, writes, writers, alone, ctx)
        set_people(entity, draws, artists, alone, ctx)
        self._set_places(entity, before, work, collections, alone, ctx)

    def _set_places(
        self,
        entity: EntityRef,
        before: Mapping[str, Any],
        work: Mapping[str, Any],
        collections: Sequence[str],
        alone: bool,
        ctx: IngestContext,
    ) -> None:
        """Put the work in its series (in its universe, if it has one), else straight in its
        universe, and in its collections; while it has one file, it leaves those that file
        no longer names."""
        series_name, universe_name = work.get("series"), work.get("universe")
        series = named(Series, series_name, ctx)
        universe = named(Universe, universe_name, ctx)
        if series is not None:
            ctx.contain(series, entity)
            if universe is not None:
                ctx.contain(universe, series)
                ctx.update(series, universe=universe_name)
        elif universe is not None:
            ctx.contain(universe, entity)
        wanted = [c for c in (named(Collection, n, ctx) for n in collections) if c is not None]
        for target in wanted:
            ctx.contain(target, entity)
        if not alone:
            return
        old_series = named(Series, before.get("series"), ctx)
        if old_series is not None and old_series != series:
            ctx.uncontain(old_series, entity)
            delete_if_empty(old_series, ctx)
        old_universe = named(Universe, before.get("universe"), ctx)
        if old_universe is not None and (old_universe != universe or series is not None):
            ctx.uncontain(old_universe, entity)
            delete_if_empty(old_universe, ctx)
        for name in split(before.get("collections")):
            collection = named(Collection, name, ctx)
            if collection is not None and collection not in wanted:
                ctx.uncontain(collection, entity)
                delete_if_empty(collection, ctx)


# --- what a file says ---


def book_work(relpath: str, epub: Mapping[str, Any] | None) -> dict[str, Any]:
    """A book's details from its EPUB metadata (``None``: unreadable), else its file name."""
    guess = book_name(relpath)
    meta = epub or {}
    creators: list[tuple[str, str | None]] = meta.get("creators") or []
    writers = [n for n, r in creators if r in (None, "writer")] or guess["writers"]
    artists = [n for n, r in creators if r == "artist"]
    title = meta.get("title") or guess["title"]
    return {
        "type": "book",
        "title": title,
        "writers": unique(writers),
        "artists": unique(artists),
        "series": meta.get("series") or guess["series"],
        "series_index": first_of(meta.get("series_index"), guess["series_index"]),
        "universe": None,
        "collections": list(meta.get("collections") or []),
        "keywords": list(meta.get("subjects") or []),
        "year": meta.get("year") or guess["year"],
        "publisher": meta.get("publisher"),
        "language": meta.get("language"),
        "isbn": meta.get("isbn"),
        "description": meta.get("description"),
        "link": web_link(meta.get("source")),
        "source": meta.get("publisher"),
    }


def comic_work(relpath: str, info: Mapping[str, Any] | None) -> dict[str, Any]:
    """A comic's details from its ``ComicInfo.xml`` (``None``: it has none), else its file
    name."""
    guess = comic_name(relpath)
    meta = info or {}
    series = meta.get("series") or guess["series"]
    number = meta.get("number") or guess["number"]
    title = meta.get("title") or (
        f"{series} #{number}" if series and number else series or guess["title"]
    )
    web = web_link(meta.get("web"))
    return {
        "type": "comic",
        "title": title,
        "writers": unique(meta.get("writers") or []),
        "artists": unique(meta.get("artists") or []),
        "series": series,
        "number": number,
        "volume": meta.get("volume") or guess["volume"],
        "series_index": issue_number(number),
        "universe": meta.get("series_group"),
        "collections": [],
        "keywords": unique([*(meta.get("genres") or []), *(meta.get("tags") or [])]),
        "story_arc": meta.get("story_arc"),
        "year": meta.get("year") or guess["year"],
        "publisher": meta.get("publisher"),
        "imprint": meta.get("imprint"),
        "description": meta.get("summary"),
        "link": web,
        "source": urlsplit(web).hostname if web else None,
    }


YEAR = re.compile(r"\s*\((\d{4})\)\s*$")
"""A trailing ``(2020)``."""
SERIES_PREFIX = re.compile(r"^(?P<series>.+?)\s+(?P<index>\d{1,3}(?:\.\d+)?)\s+-\s+(?P<title>.+)$")
"""``Discworld 03 - Equal Rites``."""
COMIC_NAME = re.compile(
    r"^(?P<series>.+?)(?:\s+v(?:ol\.?\s*)?(?P<volume>\d+))?\s+#?(?P<number>\d+(?:\.\d+)?)"
    r"(?:\s*[-:]\s*(?P<title>.+))?$",
    re.IGNORECASE,
)
"""``Saga #012``, ``Saga v2 012``, ``Saga 012 - The Will``."""


def book_name(relpath: str) -> dict[str, Any]:
    """What a book's file name says: ``Author - Title (Year)``, ``Series 03 - Title``, or
    just a title."""
    stem = posixpath.splitext(relpath.rpartition("/")[2])[0].replace("_", " ").strip()
    year = None
    found = YEAR.search(stem)
    if found:
        year, stem = int(found.group(1)), stem[: found.start()].strip()
    name: dict[str, Any] = {
        "title": stem,
        "writers": [],
        "series": None,
        "series_index": None,
        "year": year,
    }
    series = SERIES_PREFIX.match(stem)
    if series:
        name["series"] = series.group("series").strip()
        name["series_index"] = float(series.group("index"))
        name["title"] = series.group("title").strip()
    elif " - " in stem:
        author, _, title = stem.partition(" - ")
        name["writers"], name["title"] = [author.strip()], title.strip()
    return name


def comic_name(relpath: str) -> dict[str, Any]:
    """What a comic's file name says: ``Series #012 (2020)``, ``Series v2 012``."""
    stem = posixpath.splitext(relpath.rpartition("/")[2])[0].replace("_", " ").strip()
    year = None
    found = YEAR.search(stem)
    if found:
        year, stem = int(found.group(1)), stem[: found.start()].strip()
    name: dict[str, Any] = {
        "title": stem,
        "series": None,
        "number": None,
        "volume": None,
        "year": year,
    }
    match = COMIC_NAME.match(stem)
    if match:
        name["series"] = match.group("series").strip()
        number = match.group("number")
        name["number"] = (number.lstrip("0") or "0") if "." not in number else number
        name["volume"] = int(match.group("volume")) if match.group("volume") else None
        if match.group("title"):
            name["title"] = match.group("title").strip()
    return name


def issue_number(number: str | None) -> float | None:
    """A comic's number as a number, for reading order: ``"012"`` → 12, ``"1.5"`` → 1.5;
    ``None`` for ``"Annual 1"``."""
    try:
        return float(number) if number else None
    except ValueError:
        return None


def web_link(text: str | None) -> str | None:
    """``text`` if it is a web address."""
    return text if text and re.match(r"https?://", text, re.IGNORECASE) else None


def source_of(relpath: str, level: Any, fallback: str | None) -> str | None:
    """Where a file came from: its folder at depth ``level`` (1 is the first folder in the
    root), else what the file itself says."""
    folders = relpath.split("/")[:-1]
    if isinstance(level, int) and 0 < level <= len(folders):
        return folders[level - 1]
    return fallback


# --- keys, names, and the ones a work is in ---


def normalize(text: str) -> str:
    """Ignore case and punctuation: ``"The Colour of Magic!"`` and ``"the colour of
    magic"`` match."""
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()


def work_key(
    kind: type[Entity], title: str, writers: Sequence[str], work: Mapping[str, Any], relpath: str
) -> str:
    """Which work a new file is: a comic by its series, volume, and number; else by title
    and first writer; else (no writer) by title within its folder."""
    if kind is Comic and work.get("series") and work.get("number"):
        series, number = normalize(work["series"]), normalize(work["number"])
        return f"comic:{series}|{work.get('volume') or ''}|{number}"
    prefix = "book" if kind is Book else "comic"
    if writers:
        return f"{prefix}:{normalize(title)}|{normalize(writers[0])}"
    return f"{prefix}:{relpath.rpartition('/')[0]}|{normalize(title)}"


def sort_name(name: str) -> str:
    """``Terry Pratchett`` → ``Pratchett, Terry``; a name with a comma is already one."""
    if "," in name or " " not in name.strip():
        return name.strip()
    first, _, last = name.strip().rpartition(" ")
    return f"{last}, {first}"


def author_ref(name: str, ctx: IngestContext) -> EntityRef:
    return ctx.upsert(Author, f"author:{normalize(name)}", title=name, sort_name=sort_name(name))


def named(kind: type[Entity], name: str | None, ctx: IngestContext) -> EntityRef | None:
    """The series, universe, or collection called ``name`` (made if needed: one made only
    to be left is deleted again as empty)."""
    if not name or not normalize(name):
        return None
    return ctx.upsert(kind, f"{kind.__name__.lower()}:{normalize(name)}", title=name)


def set_people(
    work: EntityRef, relationship: str, names: Sequence[str], alone: bool, ctx: IngestContext
) -> None:
    """Credit ``names`` on the work; while the file is its only one, those it no longer
    names lose the credit (and an author left with no work is gone)."""
    people = [author_ref(n, ctx) for n in names if normalize(n)]
    if alone:
        keep = {p.id for p in people}
        for person in ctx.related(relationship, work):
            if person.id not in keep:
                ctx.unrelate(relationship, person, work)
                if not any(ctx.related(r, person) for r in RELATIONSHIPS):
                    ctx.delete(person)
    for person in people:
        ctx.relate(relationship, person, work)


RELATIONSHIPS = ("writers", "artists", "comic_writers", "comic_artists")


def delete_if_empty(entity: EntityRef, ctx: IngestContext) -> None:
    """A series, universe, or collection left with nothing in it is gone."""
    if not ctx.contents(entity) and not ctx.linked(entity):
        ctx.delete(entity)


def unique(names: Iterable[str]) -> list[str]:
    """Each name once (ignoring case and punctuation), in order."""
    found: dict[str, str] = {}
    for name in names:
        name = " ".join(name.split())
        if normalize(name) and normalize(name) not in found:
            found[normalize(name)] = name
    return list(found.values())


def split(text: str | None) -> list[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def merged(names: Sequence[str], *more: str | None) -> list[str]:
    return unique([*names, *(m for m in more if m)])


def first_of(*values: Any) -> Any:
    return next((v for v in values if v is not None), None)
