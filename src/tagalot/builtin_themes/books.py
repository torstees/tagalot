"""Books theme: books and comics, their writers and artists, series, universes, and
collections (DESIGN.md §9, books).

```
Universe ⊃ Series ⊃ Book, Comic     Author ↔ Book, Comic (writers, artists)
Universe ⊃ Book, Comic              Collection ⊃ Book, Comic
```

- **Formats:** books are EPUB, PDF, Markdown, Word (DOCX, and legacy DOC by name only),
  OpenDocument (ODT), Pages, and link files (``.url``, ``.webloc``, ``.desktop``) for works
  that live on the web; comics are CBZ, CBR, and CB7.
- **A work and its files:** a book or comic is keyed by its title and
  first writer (a comic by its series, volume, and number), so the same novel bought from two
  sites is one work whose files are its versions. A file already linked to a work updates it
  in place. What a file says replaces what it said before only while it is the work's one
  file; with several, each adds to the work (its writers, its series).
- **Reading files** (``prepare``, a scan worker), with the :mod:`tagalot.themes.api`
  readers: an EPUB's package, a PDF's document info, Markdown front matter, an office
  document's properties, a link file's address, a comic's ``ComicInfo.xml``; else the file
  name:
  ``Author - Title (Year)``, ``Series 03 - Title``, ``Series #012 (2020)``.
- **Sources:** where a file came from: the folder at the ``source_level`` option's depth
  (``1`` for ``Humble Bundle/…``), else a comic's web address's site, else an EPUB's
  publisher. A work's ``sources`` lists its files'.
- **Keywords:** an EPUB's subjects and a comic's genres and tags become tags only through
  file keywords (DESIGN.md §7).
- **Links:** a work's ``link`` (``display="url"``) opens in the browser from its page.
- **Thumbnails:** a work shows its cover (a picture front matter names, an EPUB's named
  cover, a PDF's first page, an office document's preview, a comic's first page); a series,
  universe, or collection one of its first works'.
- **Online details** (with the keep's consent): a book with an ISBN is looked up in Open
  Library, which fills in its year, publisher, language, and description where its files
  say nothing; what a file says always wins.
- **Searching inside books:** a book is a document (``full_text``): when the keep searches
  inside documents, the text of its EPUBs, PDFs, Markdown, and office files is read after
  scans, and **In documents** finds books by what they say.
"""

import difflib
import logging
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from tagalot.themes.api import (
    DetailView,
    Entity,
    EntityRef,
    Icon,
    ImageFile,
    IngestContext,
    OnlineResponse,
    OnlineSource,
    Record,
    ResourceInfo,
    RoleImage,
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
    read_front_matter,
    read_link_file,
    read_office_info,
    read_pdf_info,
    related,
    role,
    top_values,
)

logger = logging.getLogger(__name__)

LINK_EXTENSIONS = frozenset({".url", ".webloc", ".desktop"})
"""Link files: works that live on the web (a serial), by address."""
BOOK_EXTENSIONS = (
    frozenset({".epub", ".pdf", ".md", ".markdown", ".docx", ".odt", ".pages", ".doc"})
    | LINK_EXTENSIONS
)
COMIC_EXTENSIONS = frozenset({".cbz", ".cbr", ".cb7"})
OPEN_LIBRARY = OnlineSource("Open Library", "openlibrary.org", "ISBNs")
COVER_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
"""Pictures scanned so a Markdown book's front matter can name one as its cover."""


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
    link: str | None = field("Link", search="text", display="url")
    cover: str | None = field("Cover file", editable=False, detail=False)
    """The picture front matter names (``<root id>:<path in the root>``), so the picture is
    linked whichever is read first."""
    roles = [
        role("file", kinds={"any"}, many=True, primary=True, label="Files"),
        role("cover", kinds={"image"}, thumbnail=True),
    ]
    card_lines = ("authors", "series")
    full_text = True  # its files' text can be searched (DESIGN.md §8); comics' can't


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
    link: str | None = field("Link", search="text", display="url")
    roles = [role("file", kinds={"any"}, many=True, primary=True, label="Files")]
    card_lines = ("series", "number")


WORKS: tuple[type[Entity], ...] = (Book, Comic)
CREDITS: Mapping[type[Entity], tuple[str, str]] = {
    Book: ("writers", "artists"),
    Comic: ("comic_writers", "comic_artists"),
}
"""Each work type's relationships to its writers and to its artists: a relationship joins
two types, so books and comics each have their own."""


FRONT_MATTER_KEYS: Mapping[str, tuple[str, ...]] = {
    "title": ("title",),
    "series_index": ("series_index", "number", "series_number"),
    "year": ("year",),
    "publisher": ("publisher",),
    "language": ("language", "lang"),
    "isbn": ("isbn",),
    "description": ("description", "summary"),
    "link": ("url", "link"),
}
"""A book's editable fields and the front-matter keys they are written under: the first,
unless the file already uses another."""


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
    api_version = 6  # full_text (5: online details; 3: ctx.resource_at, write_back)
    write_back = [Book]  # Markdown books' front matter (Write to file…)
    online_sources = [OPEN_LIBRARY]
    extensions = BOOK_EXTENSIONS | COMIC_EXTENSIONS | COVER_EXTENSIONS
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
            return [RoleImage("cover"), ImageFile(), Icon("file")]
        if entity_type is Comic:
            return [ImageFile(), Icon("archive")]
        if entity_type in (Series, Universe, Collection):
            return [ContentsThumbnail(), Icon("entity")]
        return super().thumbnail_chain(entity_type)

    # --- writing back (Write to file…) ---

    def front_matter(
        self,
        entity_type: type[Entity],
        item: Record,
        edited: frozenset[str],
        current: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """A Markdown book's edited fields under the keys :func:`read_front_matter` reads,
        using the one the file already has (``number``, ``lang``, ``summary``, ``link``)."""
        if entity_type is not Book:
            return {}
        values = {"title": item.title, **item.fields}
        found: dict[str, Any] = {}
        for name, keys in FRONT_MATTER_KEYS.items():
            if name not in edited:
                continue
            key = next((k for k in keys if any(c.casefold() == k for c in current)), keys[0])
            value = values.get(name)
            if isinstance(value, float) and value.is_integer():
                value = int(value)  # number: 2, not 2.0
            found[key] = value
        return found

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
                    read, details = READERS[resource.ext]
                    info = read(resource.path)
                    if resource.ext == ".desktop" and not info.get("url"):
                        continue  # an application's launcher, not a link: not a book
                    found[resource.id] = book_work(resource.relpath, details(info))
                elif resource.ext in COMIC_EXTENSIONS:
                    info = read_comic_info(resource.path)
                    found[resource.id] = comic_work(resource.relpath, info)
            except Exception as e:  # a bad file is still a work, named from its file
                fallback = book_work if resource.ext in BOOK_EXTENSIONS else comic_work
                found[resource.id] = fallback(resource.relpath, None)
                found[resource.id]["error"] = f"{type(e).__name__}: {e}"
        return found

    # --- ingest (DB writer) ---

    # --- online details ---

    def online_requests(self, entity_type: type[Entity], item: Record) -> Iterable[str]:
        """A book with an ISBN: its edition in Open Library."""
        isbn = isbn_digits(item.fields.get("isbn")) if entity_type is Book else None
        return [f"https://{OPEN_LIBRARY.host}/isbn/{isbn}.json"] if isbn else ()

    def online_details(
        self, entity: EntityRef, responses: Mapping[str, OnlineResponse], ctx: IngestContext
    ) -> None:
        """Fill in what the book's files don't say (they always win)."""
        for url, answer in responses.items():
            if not answer.ok:
                continue  # an ISBN Open Library doesn't know
            try:
                found = open_library_values(answer.json())
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                ctx.warn(None, f"couldn't read {url}: {e}")
                continue
            current = ctx.get(entity).fields
            empty = {
                name: value
                for name, value in found.items()
                if value not in (None, "") and current.get(name) in (None, "")
            }
            if empty:
                ctx.update(entity, **empty)

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            work = ctx.prepared(resource)
            if work:
                self._ingest_work(resource, dict(work), ctx)
            elif resource.ext in COVER_EXTENSIONS:  # a cover read after its book
                key = f"{resource.root_id}:{resource.relpath}"
                for book in ctx.find(Book, cover=key):
                    ctx.link(book, resource, "cover")

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
        cover = work.pop("cover", None)

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

        if kind is Book:
            set_cover(entity, resource, cover, alone, ctx)
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


def book_work(relpath: str, details: Mapping[str, Any] | None) -> dict[str, Any]:
    """A book's details from what its file says (``None``: unreadable, or says nothing),
    each missing one from its file name. ``details`` has the keys :func:`epub_details`
    gives."""
    guess = book_name(relpath)
    meta = details or {}
    return {
        "type": "book",
        "title": meta.get("title") or guess["title"],
        "writers": unique(meta.get("writers") or guess["writers"]),
        "artists": unique(meta.get("artists") or []),
        "series": meta.get("series") or guess["series"],
        "series_index": first_of(meta.get("series_index"), guess["series_index"]),
        "universe": meta.get("universe"),
        "collections": list(meta.get("collections") or []),
        "keywords": list(meta.get("keywords") or []),
        "year": meta.get("year") or guess["year"],
        "publisher": meta.get("publisher"),
        "language": meta.get("language"),
        "isbn": meta.get("isbn"),
        "description": meta.get("description"),
        "link": web_link(meta.get("link")),
        "source": meta.get("source"),
        "cover": meta.get("cover"),
    }


def epub_details(epub: Mapping[str, Any]) -> dict[str, Any]:
    """What :func:`read_epub` found, as :func:`book_work` takes it: creators without a
    role are writers, its subjects are keywords, its publisher is the source."""
    creators: list[tuple[str, str | None]] = epub.get("creators") or []
    return {
        **{k: epub.get(k) for k in ("title", "series", "series_index", "year", "publisher")},
        **{k: epub.get(k) for k in ("language", "isbn", "description")},
        "writers": [n for n, role in creators if role in (None, "writer")],
        "artists": [n for n, role in creators if role == "artist"],
        "collections": epub.get("collections") or [],
        "keywords": epub.get("subjects") or [],
        "link": epub.get("source"),
        "source": epub.get("publisher"),
    }


def pdf_details(info: Mapping[str, Any]) -> dict[str, Any]:
    """What :func:`read_pdf_info` found: its author is the writer, its subject the
    description, its keywords keywords. A PDF names no source."""
    return {
        "title": info.get("title"),
        "writers": info.get("authors") or [],
        "keywords": info.get("keywords") or [],
        "description": info.get("subject"),
        "year": info.get("year"),
    }


def markdown_details(front: Mapping[str, Any]) -> dict[str, Any]:
    """What :func:`read_front_matter` found (its ``authors`` are writers)."""
    names = ("title", "series", "series_index", "universe", "keywords", "source", "link")
    more = ("year", "publisher", "language", "description", "cover")
    return {
        **{k: front.get(k) for k in (*names, *more)},
        "writers": front.get("authors") or [],
    }


def office_details(info: Mapping[str, Any]) -> dict[str, Any]:
    """What :func:`read_office_info` found: its authors are writers, its description (else
    its subject) the description. A Pages file gives nothing: its file name names it."""
    return {
        **{k: info.get(k) for k in ("title", "keywords", "year", "language")},
        "writers": info.get("authors") or [],
        "description": info.get("description") or info.get("subject"),
    }


def link_details(info: Mapping[str, Any]) -> dict[str, Any]:
    """What :func:`read_link_file` found: its address is the work's link; a ``.desktop``
    link's name is its title, else the file name is."""
    return {"title": info.get("title"), "link": info.get("url")}


def nothing(path: str) -> dict[str, Any]:
    """A legacy Word ``.doc``: nothing is read; its file name names it."""
    return {}


READERS: Mapping[str, tuple[Callable[[str], Any], Callable[[Any], dict[str, Any]]]] = {
    ".epub": (read_epub, epub_details),
    ".pdf": (read_pdf_info, pdf_details),
    ".md": (read_front_matter, markdown_details),
    ".markdown": (read_front_matter, markdown_details),
    ".docx": (read_office_info, office_details),
    ".odt": (read_office_info, office_details),
    ".pages": (read_office_info, office_details),
    ".doc": (nothing, dict),
    ".url": (read_link_file, link_details),
    ".webloc": (read_link_file, link_details),
    ".desktop": (read_link_file, link_details),
}
"""Each book format's reader, and what turns its result into :func:`book_work`'s details."""


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


def cover_path(relpath: str, cover: str | None) -> str | None:
    """Where the picture front matter names is in the root: relative to the Markdown file's
    folder, or to the root with a leading ``/``; Obsidian's ``[[cover.jpg]]`` too. ``None``
    for a web address or a path outside the root."""
    if not cover:
        return None
    cover = cover.strip().removeprefix("![[").removeprefix("[[").removesuffix("]]")
    cover = cover.replace("\\", "/")
    if re.match(r"^[a-z][a-z0-9+.-]*:", cover, re.IGNORECASE):  # a web address, or C:/…
        return None
    folder = "" if cover.startswith("/") else relpath.rpartition("/")[0]
    path = posixpath.normpath(posixpath.join(folder, cover.lstrip("/")))
    return None if path in (".", "..") or path.startswith("../") else path


def set_cover(
    book: EntityRef, resource: ResourceInfo, cover: str | None, alone: bool, ctx: IngestContext
) -> None:
    """Link the picture a Markdown file's front matter names as the book's cover, if it is
    scanned; remember it, so a picture read later is linked then. While the file is the
    book's only one, a cover it no longer names is let go."""
    target = cover_path(resource.relpath, cover)
    if target is None:
        if alone and resource.ext in (".md", ".markdown"):
            ctx.update(book, cover=None)
            for picture_id in ctx.linked(book, "cover"):
                ctx.unlink(book, picture_id, "cover")
        return
    ctx.update(book, cover=f"{resource.root_id}:{target}")
    picture = ctx.resource_at(resource, target)
    if picture is not None:
        ctx.link(book, picture, "cover")


# --- what Open Library says ---

OPEN_LIBRARY_LANGUAGES = {
    "eng": "en", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "spa": "es", "ita": "it",
    "por": "pt", "dut": "nl", "nld": "nl", "rus": "ru", "jpn": "ja", "chi": "zh", "zho": "zh",
    "kor": "ko", "pol": "pl", "swe": "sv", "dan": "da", "nor": "no", "fin": "fi",
}  # fmt: skip
"""Open Library's (MARC) language codes as the two-letter codes EPUBs use."""


def isbn_digits(isbn: str | None) -> str | None:
    """An ISBN as its digits (and ``X``): ``978-0-7653-1178-8`` → ``9780765311788``;
    ``None`` unless it has 10 or 13."""
    digits = re.sub(r"[^0-9Xx]", "", isbn or "").upper()
    return digits if len(digits) in (10, 13) else None


def open_library_values(edition: Mapping[str, Any]) -> dict[str, Any]:
    """An Open Library edition (``/isbn/{isbn}.json``) as a book's details."""
    publishers = edition.get("publishers") or []
    year = re.search(r"\b(\d{4})\b", str(edition.get("publish_date") or ""))
    languages = [
        OPEN_LIBRARY_LANGUAGES.get(str(lang.get("key", "")).rpartition("/")[2])
        for lang in edition.get("languages") or []
    ]
    description = edition.get("description")
    if isinstance(description, Mapping):  # {"type": "/type/text", "value": "…"}
        description = description.get("value")
    return {
        "year": int(year.group(1)) if year else None,
        "publisher": publishers[0] if publishers and isinstance(publishers[0], str) else None,
        "language": next((lang for lang in languages if lang), None),
        "description": " ".join(description.split()) if isinstance(description, str) else None,
    }


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
