"""The music theme: artists, albums, and songs from folders and tags (#98, #99)."""

import os
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import aliased

from tagalot.builtin_themes.music import (
    Album,
    Artist,
    MusicTheme,
    Song,
    album_folder,
    best_bitrate,
    from_file_name,
    genre_list,
    normalize,
    playlist_path,
    read_tags,
)
from tagalot.core.actions import ActionResult, run_action
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, EntityContains, EntityResource, Resource, ResourceStatus
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.search import child_hits, run_search
from tagalot.core.search_fields import contents_order, search_fields, view_spec
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tags import TagTreeCache
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.thumbnails.cache import ThumbCache
from tagalot.core.thumbnails.resolve import ThumbnailResolver
from tagalot.core.writer import DbWriter
from tagalot.themes.api import EntityRef, ResourceInfo, SearchView
from tagalot.themes.loader import validate_theme
from tests.core.media_files import png_bytes, write_flac, write_image, write_mp3

T0 = datetime(2026, 9, 1, tzinfo=UTC)

KIND_OF_BLUE = {"artist": "Miles Davis", "album": "Kind of Blue", "date": "1959", "genre": "Jazz"}
BOX = {"artist": "Queen", "albumartist": "Queen", "album": "The Box"}


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path

    def scan(self, when: datetime = T0) -> ScanReport:
        root = RootConfig("r", "Music", str(self.files))
        return scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=MusicTheme,
            schema=self.schema,
        )  # fmt: skip

    def titles(self, entity: type) -> list[str]:
        type_id = MusicTheme.type_id_of(entity)
        with self.reader.connect() as conn:
            return sorted(conn.scalars(select(Entity.title).where(Entity.type == type_id)))

    def fields(self, entity: type, title: str) -> dict[str, Any]:
        table = self.schema.entities[entity].table
        type_id = MusicTheme.type_id_of(entity)
        with self.reader.connect() as conn:
            row = conn.execute(
                select(table)
                .join(Entity, Entity.id == table.c.id)
                .where(Entity.title == title, Entity.type == type_id)
            ).one()
        return dict(row._mapping)

    def contents(self) -> dict[str, list[str]]:
        """``{parent: [child titles]}`` from the containment edges."""
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

    def album(self, folder: str) -> tuple[str | None, list[str]]:
        """The artist and song titles of the album in ``folder``."""
        table = self.schema.entities[Album].table
        song = aliased(Entity)
        with self.reader.connect() as conn:
            album_id, artist = conn.execute(
                select(table.c.id, table.c.artist).where(table.c.folder == folder)
            ).one()
            songs = conn.scalars(
                select(song.title)
                .join(EntityContains, EntityContains.child_id == song.id)
                .where(EntityContains.parent_id == album_id)
            ).all()
        return artist, sorted(songs)

    def entity(self, title: str) -> int:
        with self.reader.connect() as conn:
            found = conn.scalar(select(Entity.id).where(Entity.title == title))
        assert found is not None, title
        return found

    def versions(self, title: str) -> int:
        with self.reader.connect() as conn:
            links = conn.scalars(
                select(EntityResource.resource_id).where(
                    EntityResource.entity_id == self.entity(title)
                )
            ).all()
        return len(links)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    files = tmp_path / "files"
    blue = files / "Miles Davis/Kind of Blue"
    so_what = {**KIND_OF_BLUE, "title": "So What", "tracknumber": "1"}
    write_flac(blue / "01 So What.flac", tags=so_what, seconds=545)
    write_mp3(blue / "01 So What.mp3", tags=so_what)  # another version of the same song
    write_mp3(
        blue / "02 Freddie Freeloader.mp3",
        tags={**KIND_OF_BLUE, "title": "Freddie Freeloader", "tracknumber": "2/5"},
    )
    write_image(blue / "cover.jpg", (64, 64))
    # Two discs of one album, each with a track 1 called "Intro".
    write_mp3(
        files / "Box Set/CD1/01 Intro.mp3",
        pictures=[(3, png_bytes((30, 30), "green"))],  # embedded front cover
        tags={**BOX, "title": "Intro"},
    )
    write_mp3(files / "Box Set/CD2/01 Intro.mp3", tags={**BOX, "title": "Intro"}, frames=40)
    # A compilation: no album artist, and its songs' artists differ.
    hits = {"album": "Jazz Hits"}
    write_mp3(
        files / "Jazz Hits/01.mp3", tags={**hits, "title": "Take Five", "artist": "Dave Brubeck"}
    )
    write_mp3(files / "Jazz Hits/02.mp3", tags={**hits, "title": "Blue", "artist": "Joni Mitchell"})
    # No tags: named from the file and folder.
    write_mp3(files / "Untagged/03 - Lonely Road.mp3")
    (files / "Untagged/broken.mp3").write_bytes(b"not audio at all")
    # Directly in the root: a song with no album.
    write_mp3(files / "loose.mp3", tags={"title": "Loose", "artist": "Nobody"})
    (files / "Notes").mkdir()
    (files / "Notes/readme.txt").write_text("no music here", encoding="utf-8")
    return files


@pytest.fixture
def env(tmp_path: Path, library: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "M", ThemeRef("music", 1)))
    schema = open_theme(engine, keep, MusicTheme).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, library)
    reader.dispose()


def test_the_theme_is_valid() -> None:
    assert validate_theme(MusicTheme) == []


def test_a_scan_makes_artists_albums_and_songs(env: Env) -> None:
    report = env.scan()
    assert report.ingest_errors == []
    assert env.titles(Artist) == [
        "Dave Brubeck", "Joni Mitchell", "Miles Davis", "Nobody", "Queen", "Various Artists",
    ]  # fmt: skip
    assert env.titles(Album) == ["Jazz Hits", "Kind of Blue", "The Box", "Untagged"]
    assert env.titles(Song) == [
        "Blue", "Freddie Freeloader", "Intro", "Intro", "Lonely Road", "Loose", "So What",
        "Take Five", "broken",
    ]  # fmt: skip
    assert env.contents() == {
        "Dave Brubeck": ["Take Five"],  # also under their own artist, on a compilation
        "Jazz Hits": ["Blue", "Take Five"],
        "Joni Mitchell": ["Blue"],
        "Kind of Blue": ["Freddie Freeloader", "So What"],
        "Miles Davis": ["Kind of Blue"],
        "Nobody": ["Loose"],
        "Queen": ["The Box"],
        "The Box": ["Intro", "Intro"],
        "Untagged": ["Lonely Road", "broken"],
        "Various Artists": ["Jazz Hits"],
    }


def test_tags_fill_the_fields(env: Env) -> None:
    env.scan()
    so_what = env.fields(Song, "So What")
    assert (so_what["track"], so_what["disc"], so_what["artist"]) == (1, None, "Miles Davis")
    assert (so_what["album"], so_what["year"], so_what["genre"]) == ("Kind of Blue", 1959, "Jazz")
    assert so_what["folder"] == "Miles Davis/Kind of Blue"
    assert so_what["duration"] is not None
    assert env.fields(Song, "Freddie Freeloader")["track"] == 2  # from "2/5"
    album = env.fields(Album, "Kind of Blue")
    assert (album["artist"], album["year"], album["genre"]) == ("Miles Davis", 1959, "Jazz")
    assert album["folder"] == "Miles Davis/Kind of Blue"
    assert env.fields(Album, "Jazz Hits")["artist"] == "Various Artists"
    assert env.fields(Song, "Blue")["artist"] == "Joni Mitchell"  # each song keeps its own


def test_versions_of_a_song_are_one_song(env: Env) -> None:
    env.scan()
    assert env.versions("So What") == 2  # the FLAC and the MP3
    assert env.versions("Freddie Freeloader") == 1


def test_disc_folders_belong_to_their_album(env: Env) -> None:
    env.scan()
    table = env.schema.entities[Song].table
    with env.reader.connect() as conn:
        discs = sorted(
            conn.execute(
                select(table.c.disc, table.c.folder, table.c.album)
                .join(Entity, Entity.id == table.c.id)
                .where(Entity.title == "Intro")
            ).all()
        )
    assert [tuple(d) for d in discs] == [
        (1, "Box Set/CD1", "The Box"),
        (2, "Box Set/CD2", "The Box"),
    ]
    assert env.fields(Album, "The Box")["folder"] == "Box Set"


def test_a_file_without_tags_is_named_from_its_file_and_folder(env: Env) -> None:
    report = env.scan()
    road = env.fields(Song, "Lonely Road")
    assert (road["track"], road["album"], road["artist"]) == (3, "Untagged", None)
    assert env.fields(Album, "Untagged")["artist"] is None
    assert [(w.relpath, w.message.split(":")[0]) for w in report.ingest_warnings] == [
        ("Untagged/broken.mp3", "Couldn't read its tags")
    ]


def test_moving_a_song_to_another_album(env: Env) -> None:
    env.scan()
    blue = env.entity("Blue")
    target = env.files / "Joni/Blue.mp3"
    target.parent.mkdir()
    os.replace(env.files / "Jazz Hits/02.mp3", target)
    report = env.scan(T0 + timedelta(hours=1))
    assert len(report.moves) == 1
    assert env.entity("Blue") == blue  # the same song, so its tags stay
    assert env.album("Jazz Hits") == ("Dave Brubeck", ["Take Five"])  # no longer a compilation
    assert env.album("Joni") == ("Joni Mitchell", ["Blue"])  # titled "Jazz Hits", from its tag
    assert "Various Artists" not in env.titles(Artist)  # left empty, so gone
    assert env.contents()["Dave Brubeck"] == ["Jazz Hits"]  # Take Five is on their own album
    assert env.fields(Song, "Blue")["folder"] == "Joni"


def test_retagging_moves_an_album_to_its_new_artist(env: Env) -> None:
    env.scan()
    retag = {**BOX, "albumartist": "Freddie Mercury", "title": "Intro"}
    write_mp3(env.files / "Box Set/CD1/01 Intro.mp3", tags=retag, frames=30)
    write_mp3(env.files / "Box Set/CD2/01 Intro.mp3", tags=retag, frames=50)
    env.scan(T0 + timedelta(hours=1))
    contents = env.contents()
    assert contents["Freddie Mercury"] == ["The Box"]
    assert contents["Queen"] == ["Intro", "Intro"]  # the songs' own artist is still Queen
    assert "Various Artists" in env.titles(Artist)  # Jazz Hits is still a compilation
    assert env.fields(Album, "The Box")["artist"] == "Freddie Mercury"


@pytest.mark.parametrize(
    ("filename", "guess"),
    [
        ("01 So What.mp3", (None, 1, "So What")),
        ("01 - So What.flac", (None, 1, "So What")),
        ("1-03. Blue in Green.mp3", (1, 3, "Blue in Green")),
        ("7_Seven.ogg", (None, 7, "Seven")),
        ("Intro.mp3", (None, None, "Intro")),
        ("2001 Space Odyssey.mp3", (None, None, "2001 Space Odyssey")),  # not a track number
    ],
)
def test_from_file_name(filename: str, guess: tuple[int | None, int | None, str]) -> None:
    assert from_file_name(filename) == guess


@pytest.mark.parametrize(
    ("folder", "album"),
    [
        ("Miles Davis/Kind of Blue", ("Miles Davis/Kind of Blue", None)),
        ("Box Set/CD2", ("Box Set", 2)),
        ("Box Set/Disc 3", ("Box Set", 3)),
        ("Box Set/disk-4", ("Box Set", 4)),
        ("CD1", ("CD1", None)),  # a disc folder directly in the root is an album of its own
        ("", ("", None)),
    ],
)
def test_album_folder(folder: str, album: tuple[str, int | None]) -> None:
    assert album_folder(folder) == album


def test_normalize() -> None:
    assert normalize("So What!") == normalize("so  what") == "so what"


def test_thumbnail_chains(env: Env, tmp_path: Path) -> None:
    env.scan()
    cache = ThumbCache(tmp_path / "thumbs.db")
    resolver = ThumbnailResolver(
        env.reader, env.writer, MusicTheme, cache, lambda _: str(env.files), 64
    )

    def source(title: str) -> str | None:
        result = resolver.resolve(env.entity(title))
        if result.resource_id is None:
            return result.icon
        with env.reader.connect() as conn:
            return conn.scalar(select(Resource.relpath).where(Resource.id == result.resource_id))

    try:
        cover = "Miles Davis/Kind of Blue/cover.jpg"
        intro = "Box Set/CD1/01 Intro.mp3"
        assert source("Kind of Blue") == cover  # its folder image
        assert source("So What") == cover  # no art of its own: its album's
        assert source("Miles Davis") == cover  # one of its albums'
        assert source("The Box") == intro  # no folder image: its first songs' art
        assert source("Queen") == intro
        assert source("Take Five") == "audio"  # no art anywhere
        assert source("Jazz Hits") == "dir"
        assert source("Dave Brubeck") == "entity"
    finally:
        cache.close()


def _view(env: Env, name: str, *, tree_layout: bool = False) -> list[str]:
    """The titles a view lists; ``tree_layout`` gives its tree's top level."""
    view = next(v for v in MusicTheme.views if isinstance(v, SearchView) and v.name == name)
    spec = view_spec(env.schema, view)
    if tree_layout:
        spec = replace(spec, show_contained=False, nest=True)
    tree = TagTreeCache(env.reader).get()
    with env.reader.connect() as conn:
        hits = run_search(
            conn, spec, tree, limit=None, fields=search_fields(env.schema, spec.types)
        )
    return [h.title for h in hits]


def test_views(env: Env) -> None:
    env.scan()
    assert [v.name for v in MusicTheme.views if isinstance(v, SearchView)] == [
        "Browse",
        "Artists",
        "Albums",
        "Songs",
    ]
    # Albums by artist (none first), then year.
    assert _view(env, "Albums") == ["Untagged", "Kind of Blue", "The Box", "Jazz Hits"]
    assert _view(env, "Artists") == [
        "Dave Brubeck", "Joni Mitchell", "Miles Davis", "Nobody", "Queen", "Various Artists",
    ]  # fmt: skip
    # Songs by artist, album, disc, track.
    assert _view(env, "Songs") == [
        "broken", "Lonely Road",  # no artist; no track, then track 3
        "Take Five", "Blue", "So What", "Freddie Freeloader", "Loose", "Intro", "Intro",
    ]  # fmt: skip
    # Browse's tree starts with the albums, and the song that has none.
    assert sorted(_view(env, "Browse", tree_layout=True)) == [
        "Jazz Hits", "Kind of Blue", "Loose", "The Box", "Untagged",
    ]  # fmt: skip


def test_an_albums_songs_are_in_track_order(env: Env) -> None:
    env.scan()
    tree = TagTreeCache(env.reader).get()
    album = MusicTheme.type_id_of(Album)
    sort, fields = contents_order(env.schema, album)
    assert [k.field for k in sort] == ["disc", "track", "title"]
    with env.reader.connect() as conn:

        def children(title: str, **order: object) -> list[tuple[str, int | None]]:
            hits = child_hits(conn, SearchSpec(), tree, env.entity(title), **order)  # type: ignore[arg-type]
            return [(h.title, env.fields(Song, h.title)["track"]) for h in hits]

        assert children("Kind of Blue") == [("Freddie Freeloader", 2), ("So What", 1)]
        assert children("Kind of Blue", sort=sort, fields=fields) == [
            ("So What", 1),
            ("Freddie Freeloader", 2),
        ]
    # An artist's contents (albums and songs) by year: a field both have.
    sort, _ = contents_order(env.schema, MusicTheme.type_id_of(Artist))
    assert [k.field for k in sort] == ["year", "title"]


# --- Play album (#102) ---


def _play(env: Env, tmp_path: Path, *titles: str) -> ActionResult:
    ids = [env.entity(t) for t in titles]
    return env.writer.run(
        lambda conn: run_action(
            conn,
            env.schema,
            MusicTheme(),
            "play_album",
            ids,
            root_path=lambda _: str(env.files),
            temp_dir=lambda: tmp_path / "temp",
        )
    )


def test_play_album_writes_a_playlist_in_track_order(env: Env, tmp_path: Path) -> None:
    env.scan()
    result = _play(env, tmp_path, "Kind of Blue")
    assert not result.changed  # nothing to undo
    [(_, path)] = [o for o in result.outputs if o[0] == "open"]
    assert Path(path) == tmp_path / "temp" / "Kind of Blue.m3u8"
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#EXTINF:1,Miles Davis - So What"  # the song's length: its MP3's
    assert Path(lines[2]) == env.files / "Miles Davis/Kind of Blue/01 So What.flac"  # first version
    assert lines[3] == "#EXTINF:1,Miles Davis - Freddie Freeloader"
    assert Path(lines[4]).name == "02 Freddie Freeloader.mp3"
    assert result.text == "Playing 2 songs from Kind of Blue."

    again = _play(env, tmp_path, "Kind of Blue")  # a player may still hold the first one
    assert Path(again.outputs[0][1]).name == "Kind of Blue (2).m3u8"


def test_play_album_leaves_out_songs_that_arent_here(env: Env, tmp_path: Path) -> None:
    env.scan()
    env.writer.run(
        lambda conn: conn.execute(
            update(Resource)
            .where(Resource.relpath.endswith("02 Freddie Freeloader.mp3"))
            .values(status=ResourceStatus.MISSING)
        )
    )
    result = _play(env, tmp_path, "Kind of Blue", "The Box")
    assert Path(result.outputs[0][1]).name == "2 albums.m3u8"
    assert result.text == (
        "Playing 3 songs from 2 albums. 1 not on this computer (offline or missing) were left out."
    )


def test_play_album_with_nothing_to_play(env: Env, tmp_path: Path) -> None:
    env.scan()
    env.writer.run(
        lambda conn: conn.execute(
            update(Resource)
            .where(Resource.relpath.startswith("Jazz Hits/"))
            .values(status=ResourceStatus.OFFLINE)
        )
    )
    result = _play(env, tmp_path, "Jazz Hits")
    assert result.outputs == [
        ("message", "Nothing to play: Jazz Hits has no songs on this computer.")
    ]


def test_playlist_names_are_safe_file_names(tmp_path: Path) -> None:
    class Ctx:
        def temp_path(self, name: str) -> str:
            return str(tmp_path / name)

    assert Path(playlist_path(Ctx(), 'AC/DC: "Live"?')).name == "AC DC Live.m3u8"  # type: ignore[arg-type]
    assert Path(playlist_path(Ctx(), "...")).name == "Playlist.m3u8"  # type: ignore[arg-type]


def test_songs_record_the_best_bitrate_of_their_versions(env: Env) -> None:
    blue = env.files / "Miles Davis/Kind of Blue"
    mp3 = read_tags(str(blue / "01 So What.mp3"))["bitrate"]
    assert mp3
    assert read_tags(str(blue / "01 So What.flac"))["bitrate"] is None  # no audio frames
    env.scan()
    assert env.fields(Song, "So What")["bitrate"] == mp3  # the version that has one
    freddie = read_tags(str(blue / "02 Freddie Freeloader.mp3"))["bitrate"]
    assert env.fields(Song, "Freddie Freeloader")["bitrate"] == freddie


@pytest.mark.parametrize(
    ("others", "before", "read", "expected"),
    [
        ([], 320, 128, 128),  # its only file: that file's rate
        ([7], 320, 128, 320),  # another version is better
        ([7], 128, 320, 320),
        ([7], None, 192, 192),
        ([7], 192, None, 192),
        ([], None, None, None),
    ],
)
def test_best_bitrate(
    others: list[int], before: int | None, read: int | None, expected: int | None
) -> None:
    class Ctx:
        def linked(self, entity: object, role: str | None = None) -> list[int]:
            return [1, *others]  # 1: the file being read

    resource = ResourceInfo(1, "r", "a.mp3", "file", "mp3", 10, 0, "/a.mp3")
    song = EntityRef(5, "music.song")
    got = best_bitrate(Ctx(), song, resource, {"bitrate": before}, read)  # type: ignore[arg-type]
    assert got == expected


@pytest.mark.parametrize(
    ("values", "genres"),
    [
        (["Jazz"], ["Jazz"]),
        (["Pop/Rock"], ["Pop", "Rock"]),
        (["Jazz; Blues", "jazz"], ["Jazz", "Blues"]),
        (["Rock, Pop", " "], ["Rock", "Pop"]),
        (None, []),
        ([], []),
    ],
)
def test_genres_from_tags(values: list[str] | None, genres: list[str]) -> None:
    """Every genre a file names, once each: they're its file keywords (#295)."""
    assert genre_list(values) == genres
