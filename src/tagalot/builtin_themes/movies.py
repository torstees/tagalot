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

Actors and the cast come from .nfo files (#258) and by hand (#260); posters, screenshots,
and photos with the roles (#124).
"""

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from tagalot.themes.api import (
    KIND_EXTENSIONS,
    Entity,
    EntityRef,
    IngestContext,
    Kind,
    ResourceInfo,
    Theme,
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


class Collection(Entity):
    """A folder of movies' folders (a series, a box set)."""

    roles = [role("folder", kinds={"dir"}, primary=True)]


class Movie(Entity):
    """A movie; its video files are its versions (or parts)."""

    year: int | None = field("Year", card=True, search="range")
    folder: str = field("Folder", search="text", editable=False)
    roles = [role("video", kinds={"video"}, many=True, primary=True)]
    double_click = "open_file"
    card_lines = ("year",)


class MoviesTheme(Theme):
    """Movies in folders (Plex and Kodi layouts), their collections, and their casts."""

    id, name, version = "movies", "Movies", 1
    extensions = frozenset(VIDEO | IMAGE)
    dirs = True
    entities = [Actor, Collection, Movie]
    containment = [contains(Collection, Movie)]
    relationships = [related("cast", Actor, Movie, label="Cast", reverse_label="Filmography")]

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
            except Exception as e:  # a folder we can't list: the file is still a movie
                logger.warning("Couldn't look around %s: %s", resource.relpath, e)
        return found

    # --- ingest (the DB writer) ---

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            if resource.kind == "dir":
                self._ingest_folder(resource, ctx)
            elif kind_of(resource) is Kind.VIDEO and not is_extra(resource.relpath):
                self._ingest_video(resource, ctx)

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
        if details.get("movie_folder"):
            folder_name = folder.rpartition("/")[2]
            name = movie_name(folder_name if has_year(folder_name) else stem_of(resource.relpath))
            key = f"movie:dir:{folder}"
        else:
            name = movie_name(stem_of(resource.relpath))
            key = f"movie:{folder}|{name.key}"
        values = {"year": name.year, "folder": folder}

        existing = ctx.entities_of(resource, "video")
        if existing:  # it moved, or its folder changed: the movie follows the file
            movie = existing[0]
            ctx.update(movie, title=name.title, **values)
        else:
            movie = ctx.upsert(Movie, key, title=name.title, **values)
            ctx.link(movie, resource, "video")

        parent = folder.rpartition("/")[0]
        if details.get("collection") and parent:
            ctx.contain(collection_ref(parent, ctx), movie)


def collection_key(folder: str) -> str:
    return f"collection:{folder}"


def collection_ref(folder: str, ctx: IngestContext) -> EntityRef:
    return ctx.upsert(Collection, collection_key(folder), title=folder.rpartition("/")[2])
