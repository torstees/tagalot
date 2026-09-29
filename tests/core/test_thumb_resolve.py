"""Resolving thumbnails through provider chains, with the memoized source (#74)."""

import os
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import Engine, insert, select, update

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, EntityContains, Resource, ResourceStatus
from tagalot.core.scanjob import scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.thumbnails.cache import ThumbCache
from tagalot.core.thumbnails.resolve import ThumbnailResolver, ThumbnailResult
from tagalot.core.writer import DbWriter
from tagalot.themes.api import (
    Entity as ThemeEntity,
)
from tagalot.themes.api import (
    EntityRef,
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

T0 = datetime(2026, 9, 1, tzinfo=UTC)
SIZE = 64


class Album(ThemeEntity):
    roles = [
        role("folder", kinds={"dir"}, primary=True),
        role("cover", kinds={"image"}, thumbnail=True),
    ]


class Song(ThemeEntity):
    roles = [role("audio", kinds={"audio"}, primary=True)]


class Photo(ThemeEntity):
    roles = [role("image", kinds={"image"}, primary=True)]


class AlbumsTheme(Theme):
    """Folders are albums; ``cover.*`` is an album's cover, ``.mp3`` files are its songs,
    and other images are photos."""

    id, name = "albums", "Albums"
    dirs = True
    entities = [Album, Song, Photo]
    containment = [contains(Album, Song)]

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            folder, _, name = resource.relpath.rpartition("/")
            if resource.kind == "dir":
                ctx.link(ctx.upsert(Album, resource.relpath, title=name), resource, "folder")
            elif name.startswith("cover."):
                ctx.link(ctx.upsert(Album, folder, title=folder), resource, "cover")
            elif resource.ext == ".mp3":
                song = ctx.upsert(Song, resource.relpath, title=name)
                ctx.link(song, resource, "audio")
                ctx.contain(ctx.upsert(Album, folder, title=folder), song)
            else:
                ctx.link(ctx.upsert(Photo, resource.relpath, title=name), resource, "image")

    def thumbnail_chain(self, entity_type: type[ThemeEntity]) -> Sequence[ThumbnailProvider]:
        if entity_type is Song:
            return [ParentThumbnail(), Icon("audio")]
        return super().thumbnail_chain(entity_type)


def write_image(path: Path, size: tuple[int, int] = (200, 100), color: str = "red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path
    cache: ThumbCache
    theme: type[Theme]

    def scan(self, when: datetime = T0) -> None:
        root = RootConfig("r", "Files", str(self.files))
        report = scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=self.theme,
            schema=self.schema,
        )  # fmt: skip
        assert report.ingest_errors == []

    def resolver(self, theme: type[Theme] | None = None) -> ThumbnailResolver:
        return ThumbnailResolver(
            self.reader, self.writer, theme or self.theme, self.cache, self.root_path, SIZE
        )

    def root_path(self, root_id: str) -> str:
        if root_id != "r":
            raise KeyError(root_id)
        return str(self.files)

    def entity(self, title: str) -> int:
        with self.reader.connect() as conn:
            found = conn.scalar(select(Entity.id).where(Entity.title == title))
        assert found is not None, title
        return found

    def resource(self, relpath: str) -> int:
        with self.reader.connect() as conn:
            found = conn.scalar(select(Resource.id).where(Resource.relpath == relpath))
        assert found is not None, relpath
        return found

    def memo(self, entity_id: int) -> int | None:
        self.writer.run(lambda conn: None)  # memos are queued; let them land
        with self.reader.connect() as conn:
            return conn.scalar(select(Entity.thumb_resource_id).where(Entity.id == entity_id))

    def set_status(self, relpath: str, status: ResourceStatus) -> None:
        self.writer.run(
            lambda conn: conn.execute(
                update(Resource).where(Resource.relpath == relpath).values(status=status)
            )
        )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "A", ThemeRef("albums", 1)))
    schema = open_theme(engine, keep, AlbumsTheme).schema
    files = tmp_path / "files"
    write_image(files / "Blue Train/cover.jpg", (300, 300), "blue")
    (files / "Blue Train/01 Moment.mp3").write_bytes(os.urandom(300))
    (files / "Kind of Blue").mkdir()
    (files / "Kind of Blue/01 So What.mp3").write_bytes(os.urandom(300))  # no cover
    write_image(files / "beach.png", (200, 100))
    reader = create_keep_engine(keep.db_path, read_only=True)
    cache = ThumbCache(tmp_path / "k" / "thumbs.db")
    with DbWriter(engine) as writer:
        environment = Env(writer, reader, schema, files, cache, AlbumsTheme)
        environment.scan()
        yield environment
    cache.close()
    reader.dispose()


def picture_size(result: ThumbnailResult) -> tuple[int, int]:
    assert result.thumbnail is not None, result
    return result.thumbnail.width, result.thumbnail.height


# --- chains ---


def test_an_image_entity_shows_its_own_file(env: Env) -> None:
    photo = env.entity("beach.png")
    result = env.resolver().resolve(photo)
    assert picture_size(result) == (64, 32)
    assert result.icon is None
    assert result.resource_id == env.resource("beach.png")
    assert env.memo(photo) == env.resource("beach.png")


def test_a_role_marked_for_thumbnails_comes_first(env: Env) -> None:
    result = env.resolver().resolve(env.entity("Blue Train"))
    assert result.resource_id == env.resource("Blue Train/cover.jpg")
    assert picture_size(result) == (64, 64)


def test_a_song_shows_its_albums_cover(env: Env) -> None:
    song = env.entity("01 Moment.mp3")
    result = env.resolver().resolve(song)
    assert result.resource_id == env.resource("Blue Train/cover.jpg")
    assert env.memo(song) == env.resource("Blue Train/cover.jpg")
    assert env.memo(env.entity("Blue Train")) == env.resource("Blue Train/cover.jpg")


def test_without_a_picture_the_chain_ends_in_its_icon(env: Env) -> None:
    resolver = env.resolver()
    album = resolver.resolve(env.entity("Kind of Blue"))
    assert (album.thumbnail, album.icon, album.resource_id) == (None, "dir", None)
    song = resolver.resolve(env.entity("01 So What.mp3"))
    assert (song.thumbnail, song.icon) == (None, "audio")
    assert env.memo(env.entity("01 So What.mp3")) is None


def test_an_unknown_entity_gets_the_generic_icon(env: Env) -> None:
    assert env.resolver().resolve(999_999).icon == "entity"


# --- the memo and the cache ---


def test_the_memo_and_cache_spare_the_chain_and_the_file(env: Env) -> None:
    song = env.entity("01 Moment.mp3")
    env.resolver().resolve(song)
    cover = env.files / "Blue Train/cover.jpg"
    stat = cover.stat()
    cover.write_bytes(b"x" * stat.st_size)  # unreadable now, but size and time unchanged
    os.utime(cover, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    result = env.resolver().resolve(song)  # a fresh resolver: no memory of its own
    assert picture_size(result) == (64, 64)
    assert result.problems == ()


def test_a_changed_file_gets_a_new_thumbnail(env: Env) -> None:
    photo = env.entity("beach.png")
    assert picture_size(env.resolver().resolve(photo)) == (64, 32)
    write_image(env.files / "beach.png", (100, 200))
    os.utime(env.files / "beach.png", ns=(1, 1_800_000_000_000_000_000))
    env.scan(T0 + timedelta(hours=1))
    assert picture_size(env.resolver().resolve(photo)) == (32, 64)


def test_a_new_cover_is_chosen_afresh(env: Env) -> None:
    album = env.entity("Kind of Blue")
    assert env.resolver().resolve(album).icon == "dir"
    write_image(env.files / "Kind of Blue/cover.png", (50, 100))
    env.scan(T0 + timedelta(hours=1))
    result = env.resolver().resolve(album)
    assert result.resource_id == env.resource("Kind of Blue/cover.png")


def test_linking_clears_the_memo(env: Env) -> None:
    album = env.entity("Blue Train")
    env.resolver().resolve(album)
    assert env.memo(album) is not None
    write_image(env.files / "Blue Train/cover.png", (10, 10))  # replaces the single cover
    env.scan(T0 + timedelta(hours=1))
    assert env.memo(album) is None
    assert env.resolver().resolve(album).resource_id == env.resource("Blue Train/cover.png")


def test_an_offline_file_shows_its_cached_thumbnail(env: Env) -> None:
    photo = env.entity("beach.png")
    env.resolver().resolve(photo)
    env.set_status("beach.png", ResourceStatus.OFFLINE)
    assert picture_size(env.resolver().resolve(photo)) == (64, 32)


def test_an_offline_file_without_a_thumbnail_keeps_its_memo(env: Env) -> None:
    photo = env.entity("beach.png")
    env.resolver().resolve(photo)
    env.cache.clear()
    env.set_status("beach.png", ResourceStatus.OFFLINE)
    result = env.resolver().resolve(photo)
    assert (result.thumbnail, result.icon) == (None, "image")
    assert env.memo(photo) == env.resource("beach.png")  # offline isn't gone


def test_a_missing_file_is_skipped(env: Env) -> None:
    album = env.entity("Blue Train")
    env.set_status("Blue Train/cover.jpg", ResourceStatus.MISSING)
    result = env.resolver().resolve(album)
    assert (result.thumbnail, result.icon) == (None, "dir")


# --- failures ---


def test_an_unreadable_image_is_reported_once_and_skipped(env: Env) -> None:
    (env.files / "beach.png").write_bytes(b"not an image")
    os.utime(env.files / "beach.png", ns=(1, 1_800_000_000_000_000_000))
    env.scan(T0 + timedelta(hours=1))
    resolver = env.resolver()
    photo = env.entity("beach.png")
    first = resolver.resolve(photo)
    assert (first.thumbnail, first.icon) == (None, "image")
    assert [path for path, _ in first.problems] == [str(env.files / "beach.png")]
    second = resolver.resolve(photo)
    assert second.problems == ()  # not read again until it changes
    resolver.forget_failures()  # as clearing the cache does
    assert len(resolver.resolve(photo).problems) == 1


class Broken(ThumbnailProvider):
    id = "broken"

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        raise RuntimeError("oops")


class BrokenFirst(AlbumsTheme):
    def thumbnail_chain(self, entity_type: type[ThemeEntity]) -> Sequence[ThumbnailProvider]:
        return [Broken(), *super().thumbnail_chain(entity_type)]


def test_a_failing_provider_is_reported_and_skipped(env: Env) -> None:
    result = env.resolver(BrokenFirst).resolve(env.entity("beach.png"))
    assert picture_size(result) == (64, 32)
    [(_, message)] = result.problems
    assert message.endswith("failed: oops")


class ParentsAll(AlbumsTheme):
    def thumbnail_chain(self, entity_type: type[ThemeEntity]) -> Sequence[ThumbnailProvider]:
        return [ParentThumbnail(), Icon(entity_type.__name__.lower())]


def test_a_containment_cycle_does_not_loop(env: Env) -> None:
    album, song = env.entity("Kind of Blue"), env.entity("01 So What.mp3")
    env.writer.run(
        lambda conn: conn.execute(insert(EntityContains).values(parent_id=song, child_id=album))
    )
    result = env.resolver(ParentsAll).resolve(song)
    assert (result.thumbnail, result.icon) == (None, "song")


def test_a_custom_provider_can_choose_any_resource(env: Env) -> None:
    beach = env.resource("beach.png")

    class Beach(ThumbnailProvider):
        id = "beach"

        def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
            return ctx.resources(EntityRef(env.entity("beach.png"), "albums.photo"))

    class BeachEverywhere(AlbumsTheme):
        def thumbnail_chain(self, entity_type: type[ThemeEntity]) -> Sequence[ThumbnailProvider]:
            return [Beach()]

    result = env.resolver(BeachEverywhere).resolve(env.entity("Kind of Blue"))
    assert result.resource_id == beach
