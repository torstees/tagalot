"""Movies theme: Collection ⊃ Movie, and Actor ↔ Movie (the cast).

A movie is one or more video files (#123), the way Plex and Kodi lay them out:

- **Names** give the title and year: ``Inception (2010).mkv``, ``Inception.2010.1080p.mkv``.
  Files whose names differ only in a part, quality, or edition suffix (``Heat (1995) -
  720p.mkv``, ``Heat (1995) cd2.avi``) are one movie, and its files are its versions.
- **A movie's folder:** a folder whose videos are all one movie, or a folder named like a
  movie (``Inception (2010)/``), is that movie's: its name gives the title and year, and
  every video in it is one of the movie's files. Videos side by side in a folder that is
  neither are separate movies.
- **Extras** aren't movies: files named ``…-trailer``, ``…-sample`` and the like, and videos
  in ``Extras``, ``Featurettes``, ``Trailers`` (and similar) folders.
- **Collections:** a folder below the root holding two or more movies' folders
  (``The Lord of the Rings/`` with a folder per film) is a collection containing them.

**Pictures** (#124):

- In a movie's folder, ``poster.jpg`` is its **poster** (else ``…-poster``, ``folder``,
  ``cover``, ``movie``, ``default``, or a picture named like the movie), and its other
  pictures, and those in a ``screenshots``, ``extrafanart``, ``stills``, or ``backdrops``
  subfolder, are **screenshots**.
- Beside loose videos, ``Heat (1995)-poster.jpg`` (or ``Heat (1995).jpg``) is that movie's
  poster, and ``Heat (1995)-fanart.jpg`` (``-landscape``, ``-thumb``…) a screenshot.
- A collection's folder picture (``poster``, ``folder``, ``cover``) is its poster.
- An actor's photo is Kodi's ``.actors/First_Last.jpg`` in a movie's folder (#258).

**Details from .nfo files** (#258): ``movie.nfo`` in a movie's folder, or an .nfo named like
a movie's video, is Kodi's XML. It gives the title, year, plot, genre, director, runtime,
and rating, which win over what the file name says (values the user edited still win over
both); its ``<actor>`` names are the cast (actors keyed by name, so one actor's movies meet
on their page); and its ``<set>`` puts the movie in a collection of that name. Reading it
again replaces the cast and set it gave before; an actor left in no movie, with no photo,
is deleted. Cast added by hand comes with #260.

**Video details** (#259, theme version 2): each video is read with MediaInfo (pymediainfo)
in ``prepare()``: its length, picture size, video codec, and audio languages. A movie with
several versions shows its best one's (the most pixels), and the longest runtime; an .nfo's
runtime wins over the files'.
"""

import logging
import os
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from tagalot.themes.api import (
    KIND_EXTENSIONS,
    DetailView,
    Entity,
    EntityRef,
    Icon,
    IngestContext,
    Kind,
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
    kind_of,
    related,
    role,
)

logger = logging.getLogger(__name__)

VIDEO = KIND_EXTENSIONS[Kind.VIDEO]
IMAGE = KIND_EXTENSIONS[Kind.IMAGE]

EXTRAS_FOLDERS = frozenset(
    {
        "extras",
        "featurettes",
        "behind the scenes",
        "deleted scenes",
        "interviews",
        "scenes",
        "shorts",
        "trailers",
        "other",
        "samples",
        "sample",
    }
)
"""Folders whose videos are a movie's extras, not movies (Plex's names, any case)."""
EXTRA_SUFFIX = re.compile(
    r"[-_. ](trailer|sample|behindthescenes|deleted|featurette|interview|scene|short|other)$",
    re.IGNORECASE,
)
"""``Inception (2010)-trailer``: an extra, not a movie."""
YEAR_IN_PARENS = re.compile(r"^(?P<title>.*?)\s*[(\[](?P<year>(?:19|20)\d\d)[)\]](?P<rest>.*)$")
"""``Title (2010)`` or ``Title [2010]``, then anything."""
SPLIT = re.compile(r"[.\s_]+")
YEAR = re.compile(r"^(?:19|20)\d\d$")
SUFFIX = re.compile(
    r"\s*(?:[-\u2013]\s*|\s)"
    r"(?:(?:cd|dvd|disc|disk|part|pt)\s*\d+|\d{3,4}p|4k|uhd|hd|sd|"
    r"director'?s cut|extended(?: edition| cut)?|unrated|remastered|theatrical(?: cut)?)$",
    re.IGNORECASE,
)
"""A part, quality, or edition at the end of a name: ``- 1080p``, `` cd2``, ``- Extended``."""
SCREENSHOT_FOLDERS = frozenset({"screenshots", "screens", "extrafanart", "stills", "backdrops"})
"""Subfolders of a movie's folder whose pictures are its screenshots."""
ACTORS_FOLDER = ".actors"
"""Kodi's folder of actors' photos (``First_Last.jpg``)."""
NFO = ".nfo"
NFO_LIMIT = 1_000_000
"""Bytes of an .nfo read at most (they're a few KB; anything bigger isn't one)."""
POSTER_NAMES = ("poster", "folder", "cover", "movie", "default")
"""A movie's (or collection's) poster in its folder, best first (after ``…-poster``)."""
PICTURE_SUFFIX = re.compile(
    r"^(?P<base>.*?)[-._ ](?P<kind>poster|fanart|landscape|thumb|banner|background|backdrop)$",
    re.IGNORECASE,
)
"""``Heat (1995)-poster``: a loose movie's picture, and which."""


@dataclass(frozen=True)
class MovieName:
    title: str
    year: int | None

    @property
    def key(self) -> str:
        """Ignoring case and punctuation: ``Heat (1995)`` and ``heat.1995`` match."""
        return f"{normalize(self.title)}|{self.year or ''}"


def normalize(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()


def movie_name(stem: str) -> MovieName:
    """A file or folder name's title and year: ``Inception (2010) - 1080p`` → Inception,
    2010; ``The.Matrix.1999.1080p.BluRay`` → The Matrix, 1999; ``Heat cd2`` → Heat."""
    match = YEAR_IN_PARENS.match(stem)
    if match and match["title"].strip():
        return MovieName(match["title"].strip(), int(match["year"]))
    words = [w for w in SPLIT.split(stem) if w]
    # Scene style: the last year that isn't the first word ends the title.
    for i in range(len(words) - 1, 0, -1):
        if YEAR.match(words[i]):
            return MovieName(" ".join(words[:i]), int(words[i]))
    title = stem
    while True:  # "Heat - Extended - cd2"
        shorter = SUFFIX.sub("", title)
        if shorter == title or not shorter.strip():
            break
        title = shorter
    return MovieName(" ".join(w for w in SPLIT.split(title) if w) or stem, None)


def has_year(name: str) -> bool:
    """Whether a folder is named like a movie (``Inception (2010)``)."""
    match = YEAR_IN_PARENS.match(name)
    return bool(match and match["title"].strip())


def is_extra(relpath: str) -> bool:
    """A trailer, sample, or the like: in an extras folder, or named as one."""
    parts = relpath.split("/")
    if any(p.casefold() in EXTRAS_FOLDERS for p in parts[:-1]):
        return True
    stem = os.path.splitext(parts[-1])[0]
    return bool(EXTRA_SUFFIX.search(stem)) or stem.casefold() == "sample"


def stem_of(relpath: str) -> str:
    return os.path.splitext(relpath.rpartition("/")[2])[0]


# --- reading folders (prepare, in a worker) ---


def videos_in(path: str) -> list[str]:
    """The names of the movie videos directly in a folder (not extras)."""
    try:
        with os.scandir(path) as entries:
            return sorted(
                e.name
                for e in entries
                if e.is_file()
                and os.path.splitext(e.name)[1].lower() in VIDEO
                and not is_extra(e.name)
            )
    except OSError:
        return []


def images_in(path: str) -> list[str]:
    """The names of the pictures directly in a folder."""
    try:
        with os.scandir(path) as entries:
            return sorted(
                e.name
                for e in entries
                if e.is_file() and os.path.splitext(e.name)[1].lower() in IMAGE
            )
    except OSError:
        return []


def poster_rank(stem: str, movie: "MovieName | None" = None) -> int | None:
    """How good a picture in a movie's (or collection's) folder is as its poster (lower is
    better), or ``None`` for one that isn't a poster."""
    name = stem.casefold()
    if name == "poster":
        return 0
    if name.endswith(("-poster", ".poster", "_poster", " poster")):
        return 1
    if name in POSTER_NAMES:
        return 2 + POSTER_NAMES.index(name)
    if movie is not None and movie_name(stem).key == movie.key:
        return 10
    return None


def best_poster(names: Sequence[str], movie: "MovieName | None" = None) -> str | None:
    """The folder's poster among these picture names, if any is one."""
    ranked = [(r, n) for n in names if (r := poster_rank(stem_of(n), movie)) is not None]
    return min(ranked)[1] if ranked else None


def subfolders(path: str) -> list[str]:
    try:
        with os.scandir(path) as entries:
            return sorted(e.name for e in entries if e.is_dir())
    except OSError:
        return []


def is_movie_folder(name: str, videos: Sequence[str]) -> bool:
    """A folder of one movie: named like one, or all its videos are one."""
    if not videos or name.casefold() in EXTRAS_FOLDERS:
        return False
    return has_year(name) or len({movie_name(stem_of(v)).key for v in videos}) == 1


def is_collection(path: str, cache: dict[str, bool] | None = None) -> bool:
    """A folder holding two or more movies' folders."""
    cache = {} if cache is None else cache
    if path not in cache:
        found = 0
        for name in subfolders(path):
            if is_movie_folder(name, videos_in(os.path.join(path, name))):
                found += 1
                if found >= 2:
                    break
        cache[path] = found >= 2
    return cache[path]


class Actor(Entity):
    """Someone in movies' casts."""

    title_label = "Name"
    born: date | None = field("Date of birth", search="range")
    country: str | None = field("Country", search="choice")

    roles = [role("photo", kinds={"image"}, thumbnail=True)]


class Collection(Entity):
    """A folder of movies' folders (a series, a box set)."""

    contents_sort = (SortBy("year"), SortBy("title"))

    roles = [
        role("folder", kinds={"dir"}, primary=True),
        role("poster", kinds={"image"}, thumbnail=True),
    ]


class Movie(Entity):
    """A movie; its video files are its versions (or parts)."""

    year: int | None = field("Year", card=True, search="range")
    genre: str | None = field("Genre", search="text")
    """From the .nfo; several are joined with commas."""
    director: str | None = field("Director", search="text")
    runtime: float | None = field("Runtime", search="range", display="duration")
    """Seconds (an .nfo gives minutes; else the longest video's)."""
    rating: float | None = field("Rating", search="range")
    plot: str | None = field("Plot", search="text")
    quality: str | None = field("Quality", card=True, search="choice", editable=False)
    """``4K``, ``1440p``, ``1080p``, ``720p``, or ``SD``, from the best version's picture."""
    resolution: str | None = field("Resolution", editable=False)
    """The best version's picture size, ``1920 x 1080``."""
    video_codec: str | None = field("Video codec", search="choice", editable=False)
    audio: str | None = field("Audio", search="text", editable=False)
    """The best version's audio languages, ``English, French``."""
    pixels: int | None = field("Pixels", editable=False, detail=False)
    """The best version's picture size in pixels (to compare versions)."""
    folder: str = field("Folder", search="text", editable=False)
    roles = [
        role("video", kinds={"video"}, many=True, primary=True),
        role("poster", kinds={"image"}, thumbnail=True),
        role("screenshot", kinds={"image"}, many=True),
        role("nfo", kinds={"any"}, label="Details file"),
    ]
    double_click = "open_file"
    card_lines = ("year", "quality")


@dataclass(frozen=True)
class Identity:
    """Which movie a file belongs to: its key, name, and the folder of its videos."""

    key: str
    name: MovieName
    folder: str


def movie_identity(folder: str, movie_folder: bool, video_stem: str) -> Identity:
    """The movie of a video named ``video_stem`` in ``folder``: a movie's folder's own (named
    by the folder when it has a year), else the one its name gives."""
    if movie_folder:
        folder_name = folder.rpartition("/")[2]
        name = movie_name(folder_name if has_year(folder_name) else video_stem)
        return Identity(f"movie:dir:{folder}", name, folder)
    name = movie_name(video_stem)
    return Identity(f"movie:{folder}|{name.key}", name, folder)


class ContentsThumbnail(ThumbnailProvider):
    """A collection without a poster shows one of its first movies'."""

    id = "movies.contents"
    tried = 5

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        for child in ctx.children(entity)[: self.tried]:
            found = ctx.thumbnail_of(child)
            if found is not None:
                yield found


class MoviesTheme(Theme):
    """Movies in folders (Plex and Kodi layouts), their collections, and their casts."""

    id, name, version = "movies", "Movies", 2
    # 2: videos' details (runtime, resolution, codec, audio); keeps read every file once.
    extensions = frozenset(VIDEO | IMAGE | {NFO})
    dirs = True
    entities = [Actor, Collection, Movie]
    containment = [contains(Collection, Movie)]
    relationships = [related("cast", Actor, Movie, label="Filmography", reverse_label="Cast")]
    views = [
        # Tags on a collection count for its movies: tag a box set "Watched" and Movies
        # with Watched lists its films.
        SearchView("Movies", [Movie], inherit_tags=True),
        SearchView("Actors", [Actor]),
        SearchView("Collections", [Collection]),
        DetailView(Actor, [Section.fields(), Section.related("cast")]),
        DetailView(
            Movie,
            [
                Section.fields(),
                Section.role("video"),
                Section.related("cast"),
                Section.gallery("screenshot"),
                Section.role("nfo"),
            ],
        ),
    ]

    def thumbnail_chain(self, entity_type: type[Entity]) -> Sequence[ThumbnailProvider]:
        """A movie shows its poster, else its first screenshot; a collection its poster, else
        one of its movies'; an actor their photo."""
        if entity_type is Movie:
            return [RoleImage("poster"), RoleImage("screenshot"), Icon("video")]
        if entity_type is Collection:
            return [RoleImage("poster"), ContentsThumbnail(), Icon("dir")]
        return super().thumbnail_chain(entity_type)

    # --- prepare (a worker): what the folders around each file say ---

    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        found: dict[int, Any] = {}
        collections: dict[str, bool] = {}
        listings: dict[str, list[str]] = {}

        def videos(path: str) -> list[str]:
            if path not in listings:
                listings[path] = videos_in(path)
            return listings[path]

        for resource in batch:
            if not resource.path:
                continue
            try:
                if resource.kind == "dir":
                    found[resource.id] = {
                        "collection": bool(resource.relpath)
                        and is_collection(resource.path, collections)
                    }
                elif kind_of(resource) is Kind.VIDEO and not is_extra(resource.relpath):
                    folder_path = os.path.dirname(resource.path)
                    folder = resource.relpath.rpartition("/")[0]
                    name = folder.rpartition("/")[2]
                    movie_folder = bool(folder) and is_movie_folder(name, videos(folder_path))
                    grandparent = folder.rpartition("/")[0]
                    found[resource.id] = {
                        "movie_folder": movie_folder,
                        "collection": movie_folder
                        and bool(grandparent)
                        and is_collection(os.path.dirname(folder_path), collections),
                    }
                    try:
                        found[resource.id]["video"] = read_video(resource.path)
                    except Exception as e:  # unreadable: still a movie, with a warning
                        found[resource.id]["error"] = f"{type(e).__name__}: {e}"
                elif kind_of(resource) is Kind.IMAGE:
                    found[resource.id] = self._picture(resource, videos, collections)
                elif resource.relpath.lower().endswith(NFO):
                    found[resource.id] = self._nfo(resource, videos)
            except Exception as e:  # a folder we can't list: the file is still a movie
                logger.warning("Couldn't look around %s: %s", resource.relpath, e)
        return found

    def _picture(
        self,
        resource: ResourceInfo,
        videos: Callable[[str], list[str]],
        collections: dict[str, bool],
    ) -> dict[str, Any]:
        """What a picture is: a movie's poster or screenshot, a collection's poster, or
        nothing (``{}``)."""
        assert resource.path is not None
        folder_path = os.path.dirname(resource.path)
        folder, _, filename = resource.relpath.rpartition("/")
        name = folder.rpartition("/")[2]
        if name.casefold() == ACTORS_FOLDER:
            return {"actor": stem_of(filename).replace("_", " ").strip()}
        if is_extra(resource.relpath):
            return {}
        if name.casefold() in SCREENSHOT_FOLDERS and folder.count("/") >= 1:
            # A screenshots folder: its pictures are its parent movie folder's.
            movie_dir, movie_path = folder.rpartition("/")[0], os.path.dirname(folder_path)
            found = videos(movie_path)
            if movie_dir and is_movie_folder(movie_dir.rpartition("/")[2], found):
                return _movie_picture(movie_identity(movie_dir, True, stem_of(found[0])), False)
            return {}
        found = videos(folder_path)
        if folder and is_movie_folder(name, found):
            identity = movie_identity(folder, True, stem_of(found[0]))
            poster = best_poster(images_in(folder_path), identity.name) == filename
            return _movie_picture(identity, poster)
        if found:  # beside loose videos: named after one of them
            match = PICTURE_SUFFIX.match(stem_of(filename))
            base = match["base"] if match else stem_of(filename)
            kind = match["kind"].casefold() if match else "poster"
            key = movie_name(base).key
            video = next((v for v in found if movie_name(stem_of(v)).key == key), None)
            if video is None:
                return {}
            identity = movie_identity(folder, False, stem_of(video))
            if kind != "poster":
                return _movie_picture(identity, False)
            siblings = [
                n
                for n in images_in(folder_path)
                if movie_name(_picture_base(stem_of(n))).key == key
                and _picture_kind(stem_of(n)) == "poster"
            ]
            best = min(siblings, key=lambda n: (PICTURE_SUFFIX.match(stem_of(n)) is None, n))
            return _movie_picture(identity, best == filename)
        if (
            folder
            and is_collection(folder_path, collections)
            and best_poster(images_in(folder_path)) == filename
        ):
            return {"collection": folder}
        return {}

    def _nfo(self, resource: ResourceInfo, videos: Callable[[str], list[str]]) -> dict[str, Any]:
        """Which movie an .nfo is for, and what it says (or why it couldn't be read)."""
        assert resource.path is not None
        folder_path = os.path.dirname(resource.path)
        folder, _, filename = resource.relpath.rpartition("/")
        found = videos(folder_path)
        stem = stem_of(filename)
        identity: Identity | None = None
        if folder and is_movie_folder(folder.rpartition("/")[2], found):
            if stem.casefold() == "movie" or any(stem_of(v) == stem for v in found):
                identity = movie_identity(folder, True, stem_of(found[0]))
        else:
            key = movie_name(stem).key
            video = next((v for v in found if movie_name(stem_of(v)).key == key), None)
            if video is not None:
                identity = movie_identity(folder, False, stem_of(video))
        if identity is None:
            return {}
        details = _movie_picture(identity, False)
        try:
            details["nfo"] = read_nfo(resource.path)
        except Exception as e:
            details["error"] = f"{type(e).__name__}: {e}"
        return details

    # --- ingest (the DB writer) ---

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            if resource.kind == "dir":
                self._ingest_folder(resource, ctx)
            elif kind_of(resource) is Kind.VIDEO and not is_extra(resource.relpath):
                self._ingest_video(resource, ctx)
            elif kind_of(resource) is Kind.IMAGE:
                self._ingest_picture(resource, ctx)
            elif resource.relpath.lower().endswith(NFO):
                self._ingest_nfo(resource, ctx)

    def _ingest_nfo(self, resource: ResourceInfo, ctx: IngestContext) -> None:
        details = ctx.prepared(resource) or {}
        if not details.get("movie"):
            return  # not named for a movie: left unlinked
        identity = Identity(
            details["movie"], MovieName(details["title"], details["year"]), details["folder"]
        )
        movie = find_movie(identity, ctx)
        ctx.link(movie, resource, "nfo")
        if details.get("error"):
            ctx.warn(resource, f"Couldn't read it: {details['error']}")
            return
        nfo: dict[str, Any] = details["nfo"]
        values = {name: nfo.get(name) for name in ("genre", "director", "rating", "plot")}
        for name in ("year", "runtime"):  # else what the file name or the videos say
            if nfo.get(name):
                values[name] = nfo[name]
        ctx.update(movie, title=nfo.get("title") or None, **values)
        self._set_cast(movie, nfo.get("actors") or [], ctx)
        self._set_collection(movie, nfo.get("set"), ctx)

    def _set_cast(self, movie: EntityRef, names: Sequence[str], ctx: IngestContext) -> None:
        """The .nfo's actors are the movie's cast: those it no longer names leave it."""
        wanted = {normalize(n): n for n in names if normalize(n)}
        actors = {key: actor_ref(name, ctx) for key, name in wanted.items()}
        keep = {a.id for a in actors.values()}
        for actor in ctx.related("cast", movie):
            if actor.id not in keep:
                ctx.unrelate("cast", actor, movie)
                if not ctx.related("cast", actor) and not ctx.linked(actor):
                    ctx.delete(actor)  # in no movie, with no photo
        for actor in actors.values():
            ctx.relate("cast", actor, movie)

    def _set_collection(self, movie: EntityRef, name: str | None, ctx: IngestContext) -> None:
        """The .nfo's ``<set>`` is a collection (titled by it) holding the movie; a set it no
        longer names lets go of it. Folder collections are the folders' business."""
        target = (
            ctx.upsert(Collection, f"set:{normalize(name)}", title=name)
            if name and normalize(name)
            else None
        )
        for collection in ctx.find(Collection):
            if collection == target or ctx.linked(collection, "folder"):
                continue
            if movie in ctx.contents(collection):
                ctx.uncontain(collection, movie)
                if not ctx.contents(collection) and not ctx.linked(collection):
                    ctx.delete(collection)
        if target is not None:
            ctx.contain(target, movie)

    def _ingest_picture(self, resource: ResourceInfo, ctx: IngestContext) -> None:
        details = ctx.prepared(resource) or {}
        if details.get("actor"):
            ctx.link(actor_ref(details["actor"], ctx), resource, "photo")
            return
        if details.get("collection"):
            ctx.link(collection_ref(details["collection"], ctx), resource, "poster")
            return
        if not details.get("movie"):
            return
        identity = Identity(
            details["movie"], MovieName(details["title"], details["year"]), details["folder"]
        )
        movie = find_movie(identity, ctx)
        ctx.link(movie, resource, "poster" if details["poster"] else "screenshot")

    def _ingest_folder(self, resource: ResourceInfo, ctx: IngestContext) -> None:
        details = ctx.prepared(resource) or {}
        key = collection_key(resource.relpath)
        if details.get("collection"):
            collection = collection_ref(resource.relpath, ctx)
            ctx.link(collection, resource, "folder")
            return
        for collection in ctx.find(Collection, ingest_key=key):
            ctx.unlink(collection, resource, "folder")
            if not ctx.contents(collection):
                ctx.delete(collection)  # no longer a collection, and nothing in it

    def _ingest_video(self, resource: ResourceInfo, ctx: IngestContext) -> None:
        details = ctx.prepared(resource) or {}
        folder = resource.relpath.rpartition("/")[0]
        identity = movie_identity(
            folder, bool(details.get("movie_folder")), stem_of(resource.relpath)
        )
        name, key = identity.name, identity.key
        values = {"year": name.year, "folder": folder}

        existing = ctx.entities_of(resource, "video")
        # It moved, or its folder changed: the movie follows the file. A new file of a
        # movie already made (another version, or its .nfo read first) joins it.
        movie = existing[0] if existing else ctx.upsert(Movie, key)
        if ctx.linked(movie, "nfo"):
            ctx.update(movie, folder=folder)  # its .nfo names it
        else:
            ctx.update(movie, title=name.title, **values)
        if details.get("error"):
            ctx.warn(resource, f"Couldn't read its details: {details['error']}")
        elif details.get("video"):
            ctx.update(movie, **best_version(ctx, movie, resource, details["video"]))
        if not existing:
            ctx.link(movie, resource, "video")

        parent = folder.rpartition("/")[0]
        if details.get("collection") and parent:
            ctx.contain(collection_ref(parent, ctx), movie)


def collection_key(folder: str) -> str:
    return f"collection:{folder}"


def collection_ref(folder: str, ctx: IngestContext) -> EntityRef:
    return ctx.upsert(Collection, collection_key(folder), title=folder.rpartition("/")[2])


def find_movie(identity: Identity, ctx: IngestContext) -> EntityRef:
    """The movie a picture belongs to: by its key, else the movie of the same name in its
    folder (made under another key), else a new one (its video comes later)."""
    found = ctx.find(Movie, ingest_key=identity.key)
    if found:
        return found[0]
    for movie in ctx.find(Movie, folder=identity.folder):
        record = ctx.get(movie)
        if MovieName(record.title, record.fields.get("year")).key == identity.name.key:
            return movie
    return ctx.upsert(
        Movie,
        identity.key,
        title=identity.name.title,
        year=identity.name.year,
        folder=identity.folder,
    )


def _movie_picture(identity: Identity, poster: bool) -> dict[str, Any]:
    return {
        "movie": identity.key,
        "title": identity.name.title,
        "year": identity.name.year,
        "folder": identity.folder,
        "poster": poster,
    }


def _picture_base(stem: str) -> str:
    match = PICTURE_SUFFIX.match(stem)
    return match["base"] if match else stem


def _picture_kind(stem: str) -> str:
    match = PICTURE_SUFFIX.match(stem)
    return match["kind"].casefold() if match else "poster"


def actor_ref(name: str, ctx: IngestContext) -> EntityRef:
    """The actor of this name (ignoring case and punctuation), made if new."""
    return ctx.upsert(Actor, f"actor:{normalize(name)}", title=name.strip())


def read_nfo(path: str) -> dict[str, Any]:
    """What a Kodi movie .nfo says: ``title``, ``year``, ``plot``, ``genre``, ``director``,
    ``runtime`` (seconds), ``rating``, ``set``, and ``actors`` (names, in order). Missing
    values are ``None``. Text after the XML (Kodi allows a scraper URL there) is ignored."""
    with open(path, "rb") as f:
        data = f.read(NFO_LIMIT)
    end = data.rfind(b"</movie>")
    root = ElementTree.fromstring(data[: end + len(b"</movie>")] if end >= 0 else data)
    if root.tag != "movie":
        raise ValueError(f"not a movie .nfo (its root is <{root.tag}>)")

    def text(tag: str) -> str | None:
        value = root.findtext(tag)
        return value.strip() or None if value else None

    def joined(tag: str) -> str | None:
        values = [e.text.strip() for e in root.findall(tag) if e.text and e.text.strip()]
        return ", ".join(dict.fromkeys(values)) or None

    year = _int(text("year")) or _int((text("premiered") or "")[:4])
    runtime = _float(text("runtime"))
    rating = _float(text("rating"))
    if rating is None:  # Kodi 17+: <ratings><rating default="true"><value>
        ratings = root.findall("ratings/rating")
        best = next((r for r in ratings if r.get("default") == "true"), None)
        best = best if best is not None else (ratings[0] if ratings else None)
        rating = _float(best.findtext("value")) if best is not None else None
    set_element = root.find("set")
    collection = None
    if set_element is not None:
        collection = (set_element.findtext("name") or set_element.text or "").strip() or None
    actors = []

    def order(actor: ElementTree.Element) -> int:
        found = _int(actor.findtext("order"))
        return 10_000 if found is None else found  # unordered ones last, as listed

    for actor in sorted(root.findall("actor"), key=order):
        name = (actor.findtext("name") or "").strip()
        if name:
            actors.append(name)
    return {
        "title": text("title"),
        "year": year,
        "plot": text("plot") or text("outline"),
        "genre": joined("genre"),
        "director": joined("director"),
        "runtime": runtime * 60 if runtime else None,
        "rating": rating,
        "set": collection,
        "actors": actors,
    }


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text else None
    except ValueError:
        return None


def _float(text: str | None) -> float | None:
    try:
        return float(text) if text else None
    except ValueError:
        return None


VIDEO_FIELDS = ("quality", "resolution", "video_codec", "audio", "pixels")


def best_version(
    ctx: IngestContext, movie: EntityRef, resource: ResourceInfo, read: Mapping[str, Any]
) -> dict[str, Any]:
    """The movie's video fields after reading one of its videos: its best version's (the
    most pixels), and the longest runtime unless an .nfo gave one. What the movie had
    counts only while it has other videos (a movie's only video sets them)."""
    before = ctx.get(movie).fields
    others = [r for r in ctx.linked(movie, "video") if r != resource.id]
    values: dict[str, Any] = {}
    kept = (before.get("pixels") or 0) if others else 0
    if (read.get("pixels") or 0) >= kept:
        values.update({name: read.get(name) for name in VIDEO_FIELDS})
    runtime = read.get("runtime")
    if ctx.linked(movie, "nfo") and before.get("runtime"):
        pass  # the .nfo's (or a video's read before it), kept
    elif runtime and (not others or runtime > (before.get("runtime") or 0)):
        values["runtime"] = runtime
    return values


def read_video(path: str) -> dict[str, Any]:
    """A video's details by MediaInfo: ``runtime`` (seconds), ``quality``, ``resolution``,
    ``pixels``, ``video_codec``, and ``audio`` (languages). Unknown ones are ``None``."""
    from pymediainfo import MediaInfo  # the bundled library loads on first use

    info = MediaInfo.parse(path)
    general = info.general_tracks[0] if info.general_tracks else None
    video = info.video_tracks[0] if info.video_tracks else None
    duration = getattr(general, "duration", None) or getattr(video, "duration", None)
    width = _int(str(getattr(video, "width", "") or "")) if video else None
    height = _int(str(getattr(video, "height", "") or "")) if video else None
    languages = []
    for track in info.audio_tracks:
        names = getattr(track, "other_language", None) or []
        name = names[0] if names else getattr(track, "language", None)
        if name and name not in languages:
            languages.append(name)
    codec = None
    if video is not None:
        codec = getattr(video, "commercial_name", None) or getattr(video, "format", None)
    return {
        "runtime": round(float(duration) / 1000, 1) if duration else None,
        "quality": quality(width, height),
        "resolution": f"{width} \u00d7 {height}" if width and height else None,
        "pixels": width * height if width and height else None,
        "video_codec": codec,
        "audio": ", ".join(languages) or None,
    }


def quality(width: int | None, height: int | None) -> str | None:
    """A picture size's quality name. Wide films are letterboxed (1920 x 800 is 1080p),
    so the width counts as much as the height."""
    if not width or not height:
        return None
    for name, (min_width, min_height) in QUALITIES:
        if width >= min_width or height >= min_height:
            return name
    return "SD"


QUALITIES = (
    ("4K", (3800, 2000)),
    ("1440p", (2500, 1400)),
    ("1080p", (1900, 1000)),
    ("720p", (1260, 700)),
)
"""Quality names by the least width or height that has them, best first."""
