"""The movies theme (#123): movies from files and folders, their versions, extras, and
collections."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import aliased

from tagalot.builtin_themes.movies import (
    Actor,
    Collection,
    Movie,
    MovieName,
    MoviesTheme,
    is_extra,
    movie_name,
)
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, EntityContains, EntityResource, Resource
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.loader import validate_theme
from tests.core.media_files import write_image

T0 = datetime(2026, 10, 1, tzinfo=UTC)

LIBRARY = [
    "Inception (2010)/Inception.mkv",  # a folder named like a movie
    "Inception (2010)/Inception (2010)-trailer.mkv",  # an extra
    "Heat (1995).mkv",  # loose, with another version
    "Heat (1995) - 720p.mkv",
    "Alien (1979).avi",
    "The Lord of the Rings/The Fellowship of the Ring (2001)/fotr.mkv",
    "The Lord of the Rings/The Two Towers (2002)/ttt part1.mkv",
    "The Lord of the Rings/The Two Towers (2002)/ttt part2.mkv",
    "The Lord of the Rings/Extras/Making of.mkv",  # an extras folder
    "Misc/home video.mp4",  # two movies side by side: not a movie's folder
    "Misc/birthday.mp4",
    "The.Matrix.1999.1080p/The.Matrix.1999.1080p.BluRay.mkv",  # one movie: its folder
]

PICTURES = [
    "Inception (2010)/poster.jpg",  # the poster
    "Inception (2010)/folder.jpg",  # a lesser poster name: a screenshot
    "Inception (2010)/screenshots/still 1.jpg",
    "Inception (2010)/.actors/Leonardo_DiCaprio.jpg",  # an actor's (#258)
    "Heat (1995)-poster.jpg",  # beside loose videos
    "Heat (1995).jpg",  # named like the movie, but -poster wins: a screenshot
    "Heat (1995)-fanart.jpg",
    "Alien.jpg",  # no year: not Alien (1979)'s
    "random.jpg",
    "Misc/birthday-poster.jpg",
    "The Lord of the Rings/folder.jpg",  # the collection's
    "The Lord of the Rings/map.jpg",
]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Inception (2010)", MovieName("Inception", 2010)),
        ("Inception (2010) - 1080p", MovieName("Inception", 2010)),
        ("Blade Runner [1982] Final Cut", MovieName("Blade Runner", 1982)),
        ("The.Matrix.1999.1080p.BluRay.x264", MovieName("The Matrix", 1999)),
        ("2001 A Space Odyssey (1968)", MovieName("2001 A Space Odyssey", 1968)),
        ("1917.2019", MovieName("1917", 2019)),
        ("Heat cd2", MovieName("Heat", None)),
        ("Heat - Extended - cd2", MovieName("Heat", None)),
        ("Heat \u2013 720p", MovieName("Heat", None)),
        ("home video", MovieName("home video", None)),
    ],
)
def test_names(name: str, expected: MovieName) -> None:
    assert movie_name(name) == expected


def test_versions_share_a_key() -> None:
    assert movie_name("Heat (1995)").key == movie_name("heat.1995.720p").key
    assert movie_name("Heat (1995)").key != movie_name("Heat (1986)").key


@pytest.mark.parametrize(
    ("relpath", "extra"),
    [
        ("Inception (2010)/Inception (2010)-trailer.mkv", True),
        ("Inception (2010)/sample.mkv", True),
        ("Inception (2010)/Featurettes/Making of.mkv", True),
        ("Inception (2010)/Inception.mkv", False),
        ("Trailer Park Boys (1999).mkv", False),
    ],
)
def test_extras(relpath: str, extra: bool) -> None:
    assert is_extra(relpath) is extra


def test_the_theme_is_valid() -> None:
    assert validate_theme(MoviesTheme) == []


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path

    def scan(self, when: datetime = T0) -> ScanReport:
        root = RootConfig("r", "Movies", str(self.files))
        return scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=MoviesTheme,
            schema=self.schema,
        )  # fmt: skip

    def titles(self, entity: type) -> list[str]:
        type_id = MoviesTheme.type_id_of(entity)
        with self.reader.connect() as conn:
            return sorted(conn.scalars(select(Entity.title).where(Entity.type == type_id)))

    def files_of(self, title: str) -> list[str]:
        with self.reader.connect() as conn:
            return sorted(
                conn.scalars(
                    select(Resource.relpath)
                    .join(EntityResource, EntityResource.resource_id == Resource.id)
                    .join(Entity, Entity.id == EntityResource.entity_id)
                    .where(Entity.title == title)
                )
            )

    def roles(self, title: str) -> dict[str, list[str]]:
        """The item's files by role."""
        with self.reader.connect() as conn:
            rows = conn.execute(
                select(EntityResource.role, Resource.relpath)
                .join(Resource, Resource.id == EntityResource.resource_id)
                .join(Entity, Entity.id == EntityResource.entity_id)
                .where(Entity.title == title)
            ).all()
        found: dict[str, list[str]] = {}
        for role_name, relpath in rows:
            found.setdefault(role_name, []).append(relpath)
        return {r: sorted(v) for r, v in sorted(found.items())}

    def year(self, title: str) -> int | None:
        table = self.schema.entities[Movie].table
        with self.reader.connect() as conn:
            return conn.scalar(
                select(table.c.year)
                .join(Entity, Entity.id == table.c.id)
                .where(Entity.title == title)
            )

    def contents(self) -> dict[str, list[str]]:
        parent, child = aliased(Entity), aliased(Entity)
        with self.reader.connect() as conn:
            rows = conn.execute(
                select(parent.title, child.title)
                .join(EntityContains, EntityContains.parent_id == parent.id)
                .join(child, child.id == EntityContains.child_id)
            ).all()
        found: dict[str, list[str]] = {}
        for title, inside in rows:
            found.setdefault(title, []).append(inside)
        return {a: sorted(v) for a, v in sorted(found.items())}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    files = tmp_path / "files"
    for relpath in LIBRARY:
        path = files / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not really a video " + relpath.encode())
    for relpath in PICTURES:
        write_image(files / relpath, (8, 12))
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "M", ThemeRef("movies", 1)))
    schema = open_theme(engine, keep, MoviesTheme).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, files)
    reader.dispose()
    engine.dispose()


def test_a_scan_makes_movies_and_collections(env: Env) -> None:
    env.scan()
    assert env.titles(Movie) == [
        "Alien",
        "Heat",
        "Inception",
        "The Fellowship of the Ring",
        "The Matrix",
        "The Two Towers",
        "birthday",
        "home video",
    ]
    assert env.year("Inception") == 2010  # from its folder's name
    assert env.year("The Matrix") == 1999
    assert env.year("home video") is None
    assert env.roles("Heat")["video"] == ["Heat (1995) - 720p.mkv", "Heat (1995).mkv"]
    assert env.roles("The Two Towers")["video"] == [
        "The Lord of the Rings/The Two Towers (2002)/ttt part1.mkv",
        "The Lord of the Rings/The Two Towers (2002)/ttt part2.mkv",
    ]
    assert env.roles("Inception")["video"] == ["Inception (2010)/Inception.mkv"]  # no trailer
    # A folder of movies' folders is a collection; the root and Misc aren't.
    assert env.titles(Collection) == ["The Lord of the Rings"]
    assert env.contents() == {
        "The Lord of the Rings": ["The Fellowship of the Ring", "The Two Towers"]
    }
    assert env.roles("The Lord of the Rings") == {
        "folder": ["The Lord of the Rings"],
        "poster": ["The Lord of the Rings/folder.jpg"],
    }


def test_posters_and_screenshots(env: Env) -> None:
    env.scan()
    assert env.roles("Inception") == {
        "poster": ["Inception (2010)/poster.jpg"],
        "screenshot": ["Inception (2010)/folder.jpg", "Inception (2010)/screenshots/still 1.jpg"],
        "video": ["Inception (2010)/Inception.mkv"],
    }
    heat = env.roles("Heat")
    assert heat["poster"] == ["Heat (1995)-poster.jpg"]
    assert heat["screenshot"] == ["Heat (1995)-fanart.jpg", "Heat (1995).jpg"]
    assert env.roles("birthday")["poster"] == ["Misc/birthday-poster.jpg"]
    assert "poster" not in env.roles("Alien")
    with env.reader.connect() as conn:
        linked = set(conn.scalars(select(EntityResource.resource_id)))
        unlinked = sorted(
            conn.scalars(
                select(Resource.relpath).where(Resource.kind == "file", Resource.id.not_in(linked))
            )
        )
    assert unlinked == [
        "Alien.jpg",
        "Inception (2010)/.actors/Leonardo_DiCaprio.jpg",
        "Inception (2010)/Inception (2010)-trailer.mkv",
        "The Lord of the Rings/Extras/Making of.mkv",
        "The Lord of the Rings/map.jpg",
        "random.jpg",
    ]


def test_thumbnail_chains() -> None:
    theme = MoviesTheme()
    assert [repr(p) for p in theme.thumbnail_chain(Movie)] == [
        "RoleImage('poster')",
        "RoleImage('screenshot')",
        "Icon('video')",
    ]
    assert repr(theme.thumbnail_chain(Actor)[0]) == "RoleImage('photo')"


def test_a_renamed_file_moves_its_movie(env: Env) -> None:
    env.scan()
    old = env.files / "Alien (1979).avi"
    old.rename(env.files / "Alien (1979) - Directors Cut.avi")
    env.scan(T0 + timedelta(hours=1))
    assert env.titles(Movie).count("Alien") == 1
    assert env.files_of("Alien") == ["Alien (1979) - Directors Cut.avi"]


def test_a_folder_that_stops_being_a_collection(env: Env) -> None:
    env.scan()
    towers = env.files / "The Lord of the Rings" / "The Two Towers (2002)"
    for video in towers.iterdir():
        video.unlink()
    towers.rmdir()
    fellowship = env.files / "The Lord of the Rings" / "The Fellowship of the Ring (2001)"
    for video in fellowship.iterdir():
        video.unlink()
    fellowship.rmdir()
    env.scan(T0 + timedelta(hours=1))
    # Missing movies stay (offline isn't deleted), so the collection keeps them.
    assert env.titles(Collection) == ["The Lord of the Rings"]
