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
    quality,
    read_nfo,
    read_video,
)
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, EntityContains, EntityResource, Resource
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.loader import validate_theme
from tests.core.media_files import write_image, write_mkv

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
    # An actor's photo makes the actor (their .nfo may be read later).
    assert env.roles("Leonardo DiCaprio") == {
        "photo": ["Inception (2010)/.actors/Leonardo_DiCaprio.jpg"]
    }
    with env.reader.connect() as conn:
        linked = set(conn.scalars(select(EntityResource.resource_id)))
        unlinked = sorted(
            conn.scalars(
                select(Resource.relpath).where(Resource.kind == "file", Resource.id.not_in(linked))
            )
        )
    assert unlinked == [
        "Alien.jpg",
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


INCEPTION_NFO = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<movie>
  <title>Inception</title>
  <year>2010</year>
  <plot>A thief who steals secrets through dreams.</plot>
  <genre>Science Fiction</genre>
  <genre>Thriller</genre>
  <director>Christopher Nolan</director>
  <runtime>148</runtime>
  <ratings>
    <rating name="imdb" max="10"><value>8.4</value></rating>
    <rating name="themoviedb" max="10" default="true"><value>8.8</value></rating>
  </ratings>
  <set><name>Dream Heists</name></set>
  <actor><name>Elliot Page</name><role>Ariadne</role><order>2</order></actor>
  <actor><name>Leonardo DiCaprio</name><role>Cobb</role><order>0</order></actor>
  <actor><name>Tom Hardy</name><role>Eames</role><order>3</order></actor>
</movie>
https://www.themoviedb.org/movie/27205
"""


def test_reading_an_nfo(tmp_path: Path) -> None:
    path = tmp_path / "movie.nfo"
    path.write_text(INCEPTION_NFO, encoding="utf-8")
    assert read_nfo(str(path)) == {
        "title": "Inception",
        "year": 2010,
        "plot": "A thief who steals secrets through dreams.",
        "genre": "Science Fiction, Thriller",
        "director": "Christopher Nolan",
        "runtime": 148 * 60,
        "rating": 8.8,  # the default rating
        "set": "Dream Heists",
        "actors": ["Leonardo DiCaprio", "Elliot Page", "Tom Hardy"],  # by order
    }
    path.write_text("<movie><premiered>1995-12-15</premiered><set>Mann</set></movie>")
    found = read_nfo(str(path))
    assert (found["year"], found["set"], found["actors"], found["title"]) == (
        1995,
        "Mann",
        [],
        None,
    )
    path.write_text("<tvshow><title>Lost</title></tvshow>")
    with pytest.raises(ValueError, match="not a movie"):
        read_nfo(str(path))


def _cast(env: Env, title: str) -> list[str]:
    cast = env.schema.relationships["cast"].table
    actor = aliased(Entity)
    with env.reader.connect() as conn:
        return sorted(
            conn.scalars(
                select(actor.title)
                .join(cast, cast.c.a_id == actor.id)
                .join(Entity, Entity.id == cast.c.b_id)
                .where(Entity.title == title)
            )
        )


def test_nfo_files_give_details_cast_and_sets(env: Env) -> None:
    (env.files / "Inception (2010)" / "movie.nfo").write_text(INCEPTION_NFO, encoding="utf-8")
    heat = env.files / "Heat (1995).nfo"
    heat.write_text(
        "<movie><title>Heat (Definitive)</title><set>Michael Mann</set>"
        "<actor><name>Al Pacino</name></actor><actor><name>Robert De Niro</name></actor>"
        "</movie>",
        encoding="utf-8",
    )
    (env.files / "Misc" / "notes.nfo").write_text("<movie/>", encoding="utf-8")  # no such movie
    env.scan()
    table = env.schema.entities[Movie].table
    with env.reader.connect() as conn:
        row = conn.execute(
            select(table).join(Entity, Entity.id == table.c.id).where(Entity.title == "Inception")
        ).one()
    assert (row.genre, row.director, row.runtime, row.rating) == (
        "Science Fiction, Thriller",
        "Christopher Nolan",
        8880.0,
        8.8,
    )
    assert env.roles("Inception")["nfo"] == ["Inception (2010)/movie.nfo"]
    assert _cast(env, "Inception") == ["Elliot Page", "Leonardo DiCaprio", "Tom Hardy"]
    # The .nfo's title wins over the file name's, and its set is a collection.
    assert "Heat (Definitive)" in env.titles(Movie)
    assert env.year("Heat (Definitive)") == 1995  # from the file name: the .nfo has none
    contents = env.contents()
    assert contents["Michael Mann"] == ["Heat (Definitive)"]
    assert contents["Dream Heists"] == ["Inception"]

    # Reading a video again keeps the .nfo's title.
    video = env.files / "Heat (1995).mkv"
    video.write_bytes(video.read_bytes() + b"more")
    env.scan(T0 + timedelta(hours=1))
    assert "Heat (Definitive)" in env.titles(Movie)

    # An edited .nfo: the cast and set follow; an actor in nothing else, without a
    # photo, goes (Leonardo DiCaprio keeps his photo, so he stays).
    heat.write_text("<movie><title>Heat</title><actor><name>Al Pacino</name></actor></movie>")
    (env.files / "Inception (2010)" / "movie.nfo").write_text(
        INCEPTION_NFO.replace("<actor><name>Leonardo DiCaprio</name>", "<x><name>-</name>")
        .replace("</role><order>0</order></actor>", "</role></x>")
        .replace("<set><name>Dream Heists</name></set>", ""),
        encoding="utf-8",
    )
    env.scan(T0 + timedelta(hours=2))
    assert _cast(env, "Heat") == ["Al Pacino"]
    assert _cast(env, "Inception") == ["Elliot Page", "Tom Hardy"]
    actors = env.titles(Actor)
    assert "Robert De Niro" not in actors
    assert "Leonardo DiCaprio" in actors
    assert "Michael Mann" not in env.titles(Collection)  # empty: gone
    assert "Dream Heists" not in env.titles(Collection)


def test_a_broken_nfo_is_still_linked(env: Env) -> None:
    (env.files / "Alien (1979).nfo").write_text("<movie><title>Alien", encoding="utf-8")
    [report] = [env.scan()]
    assert env.roles("Alien")["nfo"] == ["Alien (1979).nfo"]
    assert env.titles(Movie).count("Alien") == 1
    assert any("Couldn't read it" in w.message for w in report.ingest_warnings)


def test_reading_a_video(tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    write_mkv(path, (1920, 800), 7200, languages=["eng", "fre", "eng"])
    assert read_video(str(path)) == {
        "runtime": 7200.0,
        "quality": "1080p",  # letterboxed: the width says 1080p
        "resolution": "1920 " + chr(0xD7) + " 800",
        "pixels": 1920 * 800,
        "video_codec": "VP9",
        "audio": "English, French",
    }
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"not a video")
    found = read_video(str(junk))
    assert (found["runtime"], found["quality"], found["audio"]) == (None, None, None)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((3840, 2160), "4K"),
        ((3840, 1600), "4K"),
        ((2560, 1440), "1440p"),
        ((1920, 1080), "1080p"),
        ((1440, 1080), "1080p"),
        ((1280, 536), "720p"),
        ((720, 480), "SD"),
        ((0, 0), None),
    ],
)
def test_quality(size: tuple[int, int], expected: str | None) -> None:
    assert quality(*size) == expected


def _video_fields(env: Env, title: str) -> tuple[object, ...]:
    table = env.schema.entities[Movie].table
    with env.reader.connect() as conn:
        row = conn.execute(
            select(table).join(Entity, Entity.id == table.c.id).where(Entity.title == title)
        ).one()
    return (row.runtime, row.quality, row.resolution, row.video_codec, row.audio)


def test_videos_give_details_and_the_best_version_wins(env: Env) -> None:
    write_mkv(env.files / "Heat (1995).mkv", (1920, 1080), 10200, languages=["eng"])
    write_mkv(env.files / "Heat (1995) - 720p.mkv", (1280, 720), 10260, languages=["fre"])
    env.scan()
    # 1080p's picture, codec, and audio; the longer runtime
    assert _video_fields(env, "Heat") == (
        10260.0,
        "1080p",
        "1920 " + chr(0xD7) + " 1080",
        "VP9",
        "English",
    )
    assert _video_fields(env, "Alien")[:2] == (None, None)  # not a real video: nothing

    # An .nfo's runtime wins over the videos'.
    (env.files / "Heat (1995).nfo").write_text("<movie><runtime>170</runtime></movie>")
    env.scan(T0 + timedelta(hours=1))
    assert _video_fields(env, "Heat")[0] == 170 * 60
    video = env.files / "Heat (1995) - 720p.mkv"
    write_mkv(video, (1280, 720), 11000, languages=["fre"])  # changed: read again
    env.scan(T0 + timedelta(hours=2))
    assert _video_fields(env, "Heat")[0] == 170 * 60


def test_the_theme_reads_files_again_after_version_1() -> None:
    assert MoviesTheme.version == 2
