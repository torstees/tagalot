# mypy: disable-error-code="no-untyped-call"
# (mutagen is untyped)
"""The folder, embedded-art, and archive providers, and the audio renderer (#75)."""

import base64
import io
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen import MutagenError
from mutagen.flac import Picture
from mutagen.mp4 import MP4Cover, MP4Tags
from PIL import Image
from sqlalchemy import Engine, select

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, Resource
from tagalot.core.scanjob import scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.thumbnails.cache import ThumbCache
from tagalot.core.thumbnails.render import embedded_art, load_audio_art
from tagalot.core.thumbnails.resolve import ThumbnailResolver, ThumbnailResult
from tagalot.core.writer import DbWriter
from tagalot.themes.api import (
    FOLDER_IMAGE_NAMES,
    ArchiveFirstImage,
    EmbeddedAudioArt,
    EntityRef,
    FolderImage,
    Icon,
    IngestContext,
    ParentThumbnail,
    ResourceInfo,
    Theme,
    ThumbnailContext,
    ThumbnailProvider,
    contains,
    role,
)
from tagalot.themes.api import (
    Entity as ThemeEntity,
)
from tests.core.media_files import png_bytes, write_flac, write_image, write_mp3

T0 = datetime(2026, 9, 1, tzinfo=UTC)
SIZE = 64
FRONT, BACK = 3, 4


# --- embedded art ---


def _size(data: bytes | None) -> tuple[int, int]:
    assert data is not None
    return Image.open(io.BytesIO(data)).size


def _picture_b64(kind: int, data: bytes) -> str:
    picture = Picture()
    picture.type, picture.mime, picture.data = kind, "image/png", data
    return base64.b64encode(picture.write()).decode("ascii")


@pytest.mark.parametrize("write", [write_mp3, write_flac])
def test_the_front_cover_is_preferred(tmp_path: Path, write: object) -> None:
    path = tmp_path / "song"
    write(path, [(BACK, png_bytes((10, 10))), (FRONT, png_bytes((30, 20)))])  # type: ignore[operator]
    image = load_audio_art(str(path), SIZE)
    assert image is not None
    assert image.size == (30, 20)


@pytest.mark.parametrize("write", [write_mp3, write_flac])
def test_any_picture_when_none_is_the_front(tmp_path: Path, write: object) -> None:
    path = tmp_path / "song"
    write(path, [(BACK, png_bytes((10, 10)))])  # type: ignore[operator]
    image = load_audio_art(str(path), SIZE)
    assert image is not None
    assert image.size == (10, 10)


@pytest.mark.parametrize("write", [write_mp3, write_flac])
def test_audio_without_art(tmp_path: Path, write: object) -> None:
    path = tmp_path / "song"
    write(path)  # type: ignore[operator]
    assert load_audio_art(str(path), SIZE) is None


def test_a_file_that_isnt_audio(tmp_path: Path) -> None:
    (tmp_path / "notes.mp3").write_bytes(b"just some text, really")
    with pytest.raises(MutagenError):  # reported by the resolver, like a corrupt image
        load_audio_art(str(tmp_path / "notes.mp3"), SIZE)
    (tmp_path / "notes.txt").write_bytes(b"just some text, really")
    assert load_audio_art(str(tmp_path / "notes.txt"), SIZE) is None


def test_mp4_cover_art() -> None:
    tags = MP4Tags()
    tags["covr"] = [MP4Cover(png_bytes((12, 34)), imageformat=MP4Cover.FORMAT_PNG)]
    assert _size(embedded_art(SimpleNamespace(tags=tags))) == (12, 34)


def test_ogg_pictures_skip_broken_ones() -> None:
    tags = {
        "metadata_block_picture": [
            "not base64!",
            _picture_b64(BACK, png_bytes((5, 5))),
            _picture_b64(FRONT, png_bytes((7, 9))),
        ]
    }
    assert _size(embedded_art(SimpleNamespace(tags=tags))) == (7, 9)


def test_no_tags_at_all() -> None:
    assert embedded_art(SimpleNamespace(tags=None)) is None


# --- providers, against a stand-in context ---


def _file(relpath: str, rid: int = 1) -> ResourceInfo:
    ext = "." + relpath.rpartition(".")[2].lower() if "." in relpath else ""
    return ResourceInfo(rid, "r", relpath, "file", ext, 1, 1, relpath)


@dataclass
class FakeContext:
    files: list[ResourceInfo]
    own: dict[int, list[ResourceInfo]]
    kids: list[EntityRef]

    def resources(self, entity: EntityRef, role: str | None = None) -> list[ResourceInfo]:
        return self.own.get(entity.id, [])

    def primary_role(self, entity: EntityRef) -> str | None:
        return "main"

    def parents(self, entity: EntityRef) -> list[EntityRef]:
        return []

    def children(self, entity: EntityRef) -> list[EntityRef]:
        return self.kids

    def folder_files(
        self, entity: EntityRef, extensions: object = None
    ) -> list[ResourceInfo]:  # fmt: skip
        return self.files

    def thumbnail_of(self, entity: EntityRef) -> ResourceInfo | None:
        return None


def _names(provider: ThumbnailProvider, ctx: FakeContext) -> list[str]:
    return [r.relpath for r in provider.candidates(EntityRef(1, "t.album"), ctx)]


def test_folder_image_names_in_order_of_preference() -> None:
    files = [
        _file(name)
        for name in [
            "a/AlbumArtSmall.jpg",
            "a/AlbumArt_{7D2A}_Large.JPG",
            "a/Cover.png",
            "a/Folder.jpg",
            "a/front.webp",
            "a/folderish.jpg",
            "a/scan.jpg",
        ]
    ]
    ctx = FakeContext(files, {}, [])
    assert _names(FolderImage(), ctx) == [
        "a/Folder.jpg",
        "a/Cover.png",
        "a/front.webp",
        "a/AlbumArt_{7D2A}_Large.JPG",
        "a/AlbumArtSmall.jpg",
    ]
    assert _names(FolderImage(["scan", *FOLDER_IMAGE_NAMES[:1]]), ctx) == [
        "a/scan.jpg",
        "a/Folder.jpg",
    ]
    assert repr(FolderImage()) == "FolderImage()"
    assert repr(FolderImage(["Poster"])) == "FolderImage(['poster'])"


def test_embedded_art_prefers_own_audio_then_the_first_children() -> None:
    song = _file("a/01.mp3", 10)
    ctx = FakeContext([], {1: [song, _file("a/lyrics.txt", 11)]}, [])
    assert _names(EmbeddedAudioArt(), ctx) == ["a/01.mp3"]
    kids = [EntityRef(i, "t.song") for i in range(2, 7)]
    own = {i: [_file(f"a/0{i}.flac", i)] for i in range(2, 7)}
    own[3] = [_file("a/03.txt", 3)]  # no audio: not counted
    ctx = FakeContext([], own, kids)
    assert _names(EmbeddedAudioArt(), ctx) == ["a/02.flac", "a/04.flac", "a/05.flac"]


def test_archive_first_image_offers_the_archive() -> None:
    ctx = FakeContext([], {1: [_file("p.zip"), _file("p.txt", 2), _file("q.7z", 3)]}, [])
    assert _names(ArchiveFirstImage(), ctx) == ["p.zip", "q.7z"]


def test_the_fake_context_is_a_context() -> None:
    ctx: ThumbnailContext = FakeContext([], {}, [])
    assert ctx.primary_role(EntityRef(1, "t")) == "main"


# --- end to end ---


class Album(ThemeEntity):
    roles = [role("folder", kinds={"dir"}, primary=True)]


class Song(ThemeEntity):
    roles = [role("audio", kinds={"audio"}, primary=True)]


class MusicTheme(Theme):
    id, name = "tunes", "Tunes"
    dirs = True
    entities = [Album, Song]
    containment = [contains(Album, Song)]

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            folder, _, name = resource.relpath.rpartition("/")
            if resource.kind == "dir":
                ctx.link(ctx.upsert(Album, resource.relpath, title=name), resource, "folder")
            elif resource.ext in {".mp3", ".flac"}:
                song = ctx.upsert(Song, resource.relpath, title=name)
                ctx.link(song, resource, "audio")
                if folder:
                    ctx.contain(ctx.upsert(Album, folder, title=folder.rpartition("/")[2]), song)

    def thumbnail_chain(self, entity_type: type[ThemeEntity]) -> Sequence[ThumbnailProvider]:
        if entity_type is Album:
            return [FolderImage(), EmbeddedAudioArt(), Icon("dir")]
        return [EmbeddedAudioArt(), FolderImage(), ParentThumbnail(), Icon("audio")]


@dataclass
class Env:
    reader: Engine
    resolver: ThumbnailResolver

    def resolve(self, title: str) -> ThumbnailResult:
        with self.reader.connect() as conn:
            found = conn.scalar(select(Entity.id).where(Entity.title == title))
        assert found is not None, title
        return self.resolver.resolve(found)

    def source(self, title: str) -> str | None:
        result = self.resolve(title)
        if result.resource_id is None:
            return None
        with self.reader.connect() as conn:
            return conn.scalar(select(Resource.relpath).where(Resource.id == result.resource_id))


@pytest.fixture
def library(tmp_path: Path) -> Path:
    files = tmp_path / "files"
    write_image(files / "Blue Train/folder.jpg", (100, 100), "blue")
    write_image(files / "Blue Train/cover.jpg", (100, 100), "red")
    write_image(files / "Blue Train/scans/folder.jpg", (100, 100), "green")
    write_mp3(files / "Blue Train/01 Moment.mp3")
    write_flac(files / "Blue Train/02 Locomotion.flac", [(FRONT, png_bytes((80, 40)))])
    write_mp3(files / "Kind of Blue/02 Freddie.mp3", [(FRONT, png_bytes((40, 80)))])
    write_mp3(files / "Kind of Blue/01 So What.mp3")  # no art
    write_image(files / "100%_Hits/cover.png", (50, 50))
    write_mp3(files / "100%_Hits/hit.mp3")
    write_image(files / "100% Other/cover.png", (60, 60))
    write_mp3(files / "loose.mp3")
    write_image(files / "front.jpg", (70, 70))
    return files


@pytest.fixture
def env(tmp_path: Path, library: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "T", ThemeRef("tunes", 1)))
    schema: ThemeSchema = open_theme(engine, keep, MusicTheme).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    cache = ThumbCache(tmp_path / "k" / "thumbs.db")
    root = RootConfig("r", "Music", str(library))
    with DbWriter(engine) as writer:
        report = scan_root(
            writer, reader, root, root.path, when=T0, theme=MusicTheme, schema=schema
        )
        assert report.ingest_errors == []
        yield Env(
            reader, ThumbnailResolver(reader, writer, MusicTheme, cache, lambda _: root.path, SIZE)
        )
    cache.close()
    reader.dispose()


def test_an_album_uses_its_folder_image(env: Env) -> None:
    assert env.source("Blue Train") == "Blue Train/folder.jpg"  # not cover.jpg, not scans/


def test_an_album_without_one_uses_its_first_songs_art(env: Env) -> None:
    result = env.resolve("Kind of Blue")
    assert env.source("Kind of Blue") == "Kind of Blue/02 Freddie.mp3"
    assert result.thumbnail is not None
    assert (result.thumbnail.width, result.thumbnail.height) == (32, 64)


def test_a_song_uses_its_own_art_first(env: Env) -> None:
    assert env.source("02 Locomotion.flac") == "Blue Train/02 Locomotion.flac"
    assert env.source("02 Freddie.mp3") == "Kind of Blue/02 Freddie.mp3"


def test_a_song_without_art_uses_its_folder_image(env: Env) -> None:
    assert env.source("01 Moment.mp3") == "Blue Train/folder.jpg"
    assert env.source("01 So What.mp3") == "Kind of Blue/02 Freddie.mp3"  # via its album


def test_folder_names_with_like_wildcards(env: Env) -> None:
    assert env.source("hit.mp3") == "100%_Hits/cover.png"
    assert env.source("100%_Hits") == "100%_Hits/cover.png"


def test_a_file_at_the_top_of_a_root(env: Env) -> None:
    assert env.source("loose.mp3") == "front.jpg"
