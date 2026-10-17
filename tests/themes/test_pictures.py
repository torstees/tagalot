"""Pictures and archives as items of their own in the movies and books themes (#446)."""

import os
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import Engine, select

from tagalot.builtin_themes import books, movies
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.fields import edit_field
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, EntityResource, Resource
from tagalot.core.scanjob import scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.thumbnails.archive import open_archive
from tagalot.core.writer import DbWriter
from tagalot.themes.api import Theme, oldest, read_archive, read_picture
from tagalot.themes.readers import exif_time
from tests.core.media_files import write_image

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
OLD = datetime(2001, 2, 3, 4, 5, 6)
"""A date long before the files were written (naive: local time, as cameras record it)."""


def write_photo(path: Path, taken: str | None = None, offset: str | None = None) -> None:
    """A small JPEG whose EXIF says when it was taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (30, 20), "blue")
    exif = Image.Exif()
    if taken is not None:
        details = exif.get_ifd(0x8769)
        details[0x9003] = taken
        if offset is not None:
            details[0x9011] = offset
    image.save(path, exif=exif)


Stamp = tuple[int, int, int, int, int, int]


def write_zip(path: Path, members: list[tuple[str, Stamp]]) -> None:
    """A zip of tiny members, each with the date (a ``date_time`` tuple) given."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, when in members:
            zf.writestr(zipfile.ZipInfo(name, when), b"" if name.endswith("/") else b"x")


def local(when: datetime) -> datetime:
    return when.astimezone()


# --- reading ---


def test_a_pictures_size_and_the_date_it_was_taken(tmp_path: Path) -> None:
    write_photo(tmp_path / "p.jpg", "2001:02:03 04:05:06")
    assert read_picture(str(tmp_path / "p.jpg")) == {
        "width": 30,
        "height": 20,
        "dimensions": "30 \u00d7 20",
        "taken": local(OLD),
    }


def test_a_picture_without_exif_has_no_date(tmp_path: Path) -> None:
    write_image(tmp_path / "p.png", (5, 7))
    assert read_picture(str(tmp_path / "p.png"))["taken"] is None


def test_a_dates_offset_is_its_time_zone(tmp_path: Path) -> None:
    write_photo(tmp_path / "p.jpg", "2001:02:03 04:05:06", "+02:00")
    taken = read_picture(str(tmp_path / "p.jpg"))["taken"]
    assert taken == OLD.replace(tzinfo=timezone(timedelta(hours=2)))


@pytest.mark.parametrize("text", ["", "0000:00:00 00:00:00", "    :  :     :  :  ", None, 5])
def test_blank_or_impossible_exif_dates(text: Any) -> None:
    assert exif_time(text) is None


def test_the_oldest_date_wins() -> None:
    a, b = datetime(2001, 1, 1, tzinfo=UTC), datetime(2020, 1, 1, tzinfo=UTC)
    assert oldest(b, None, a) == a
    assert oldest(None, None) is None
    assert oldest() is None


def test_an_archives_date_is_the_oldest_of_its_first_ten_files(tmp_path: Path) -> None:
    members: list[tuple[str, Stamp]] = [("pics/", (1985, 1, 1, 0, 0, 0))]  # a folder: not counted
    members += [(f"pics/{n:02}.jpg", (2010, 1, n + 1, 0, 0, 0)) for n in range(10)]
    members += [("pics/late.jpg", (1999, 1, 1, 0, 0, 0))]  # the eleventh file: not looked at
    members += [("notes.txt", (2011, 1, 1, 0, 0, 0))]
    write_zip(tmp_path / "a.zip", members)
    assert read_archive(str(tmp_path / "a.zip")) == {
        "pictures": 11,
        "oldest": local(datetime(2010, 1, 1)),
    }
    assert read_archive(str(tmp_path / "a.zip"), dated=11)["oldest"] == local(datetime(1999, 1, 1))


def test_a_zip_without_dates_has_none(tmp_path: Path) -> None:
    write_zip(tmp_path / "a.zip", [("a.png", (1980, 1, 1, 0, 0, 0))])  # zip's "not recorded"
    assert read_archive(str(tmp_path / "a.zip"))["oldest"] is None


def test_the_archive_reader_gives_members_dates(tmp_path: Path) -> None:
    write_zip(tmp_path / "a.zip", [("a.png", (2003, 4, 5, 6, 7, 8))])
    with open_archive(str(tmp_path / "a.zip")) as archive:
        (member,) = archive.members()
    assert member.modified == local(datetime(2003, 4, 5, 6, 7, 8))


# --- the themes ---


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path
    theme: type[Theme]

    def scan(self) -> None:
        root = RootConfig("r", "Files", str(self.files))
        scan_root(
            self.writer, self.reader, root, root.path, when=T0, theme=self.theme,
            schema=self.schema,
        )  # fmt: skip

    def titles(self, kind: type) -> list[str]:
        type_id = self.theme.type_id_of(kind)
        with self.reader.connect() as conn:
            return sorted(conn.scalars(select(Entity.title).where(Entity.type == type_id)))

    def fields(self, kind: type, title: str) -> dict[str, Any]:
        table = self.schema.entities[kind].table
        with self.reader.connect() as conn:
            row = conn.execute(
                select(table).join(Entity, Entity.id == table.c.id).where(Entity.title == title)
            ).one()
        return dict(row._mapping)

    def files_of(self, title: str) -> dict[str, list[str]]:
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
        return found


def make_env(tmp_path: Path, theme: type[Theme]) -> Iterator[Env]:
    files = tmp_path / "files"
    files.mkdir()
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef(theme.id, 1)))
    schema = open_theme(engine, keep, theme).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, files, theme)
    reader.dispose()
    engine.dispose()


@pytest.fixture
def movies_env(tmp_path: Path) -> Iterator[Env]:
    yield from make_env(tmp_path, movies.MoviesTheme)


@pytest.fixture
def books_env(tmp_path: Path) -> Iterator[Env]:
    yield from make_env(tmp_path, books.BooksTheme)


def video(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not really a video")


def set_modified(path: Path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def test_family_pictures_beside_home_videos_are_pictures(movies_env: Env) -> None:
    files = movies_env.files
    video(files / "Alice/beach.mp4")
    video(files / "Alice/park.mp4")
    write_photo(files / "Alice/IMG_0001.jpg", "2001:02:03 04:05:06")
    write_image(files / "Alice/drawing.png")
    set_modified(files / "Alice/drawing.png", datetime(2005, 6, 7, tzinfo=UTC))
    write_zip(
        files / "Alice/scans.zip",
        [("a.jpg", (1995, 1, 1, 0, 0, 0)), ("b.jpg", (1996, 1, 1, 0, 0, 0))],
    )
    video(files / "Alice/3rd Birthday/party.mp4")  # one video: the movie's own folder
    write_photo(files / "Alice/3rd Birthday/cake.jpg")
    write_image(files / "Alice/3rd Birthday/fanart.jpg")  # Kodi's art: the movie's only
    write_image(files / "Alice/3rd Birthday/poster.jpg")
    movies_env.scan()

    assert movies_env.titles(movies.Picture) == ["IMG_0001.jpg", "cake.jpg", "drawing.png"]
    assert movies_env.titles(movies.Archive) == ["scans.zip"]
    photo = movies_env.fields(movies.Picture, "IMG_0001.jpg")
    assert photo["date"] == local(OLD)  # when it was taken, not when the file was written
    assert photo["dimensions"] == "30 \u00d7 20"
    assert photo["folder"] == "Alice"
    assert movies_env.fields(movies.Picture, "drawing.png")["date"] == datetime(
        2005, 6, 7, tzinfo=UTC
    )
    archive = movies_env.fields(movies.Archive, "scans.zip")
    assert archive["pictures"] == 2
    assert archive["date"] == local(datetime(1995, 1, 1))  # older than the zip itself
    # The birthday's picture is its screenshot and a picture of its own; its art is only art.
    party = movies_env.files_of("party")  # a folder without a year: named by its video
    assert party["poster"] == ["Alice/3rd Birthday/poster.jpg"]
    assert sorted(party["screenshot"]) == [
        "Alice/3rd Birthday/cake.jpg",
        "Alice/3rd Birthday/fanart.jpg",
    ]
    assert movies_env.files_of("cake.jpg") == {"picture": ["Alice/3rd Birthday/cake.jpg"]}


def test_kodi_artwork_folders_and_actors_are_not_pictures(movies_env: Env) -> None:
    files = movies_env.files
    video(files / "Heat (1995)/Heat.mkv")
    for name in ("extrafanart/1.jpg", "backdrops/2.jpg", ".actors/Al_Pacino.jpg", "banner.jpg"):
        write_image(files / "Heat (1995)" / name)
    write_image(files / "Heat (1995)/Screenshots/shot.jpg")  # stills: pictures too
    movies_env.scan()
    assert movies_env.titles(movies.Picture) == ["shot.jpg"]


def test_folder_images_are_not_pictures(movies_env: Env) -> None:
    """``folder.jpg`` and the like are a folder's look in Explorer, not pictures (#458)."""
    files = movies_env.files
    video(files / "Alice/beach.mp4")
    video(files / "Alice/park.mp4")
    write_image(files / "Alice/IMG_1.jpg")
    for name in ("folder.jpg", "Cover.PNG", "front.jpg", "AlbumArt_{1}_Large.jpg"):
        write_image(files / "Alice" / name)
    write_image(files / "Alice/! Posters/folder.jpg")
    movies_env.scan()
    assert movies_env.titles(movies.Picture) == ["IMG_1.jpg"]
    assert movies_env.titles(movies.Folder) == ["Alice"]  # ! Posters holds no picture


def test_pictures_made_before_are_taken_away(movies_env: Env, monkeypatch: Any) -> None:
    """A keep scanned by theme version 5 has its folder.jpg as a Picture: reading the file
    again takes it away."""
    write_image(movies_env.files / "Alice/folder.jpg")
    write_image(movies_env.files / "Alice/IMG_1.jpg")
    monkeypatch.setattr(movies, "is_folder_image", lambda relpath: False)
    movies_env.scan()
    assert movies_env.titles(movies.Picture) == ["IMG_1.jpg", "folder.jpg"]
    monkeypatch.undo()
    set_modified(movies_env.files / "Alice/folder.jpg", datetime(2030, 1, 1, tzinfo=UTC))
    movies_env.scan()
    assert movies_env.titles(movies.Picture) == ["IMG_1.jpg"]


def test_a_books_folder_images_are_not_pictures(books_env: Env) -> None:
    files = books_env.files
    write_image(files / "Art/folder.jpg")
    write_image(files / "Art/hero.png")
    books_env.scan()
    assert books_env.titles(books.Picture) == ["hero.png"]
    assert books_env.titles(books.Folder) == ["Art"]


def test_a_picture_and_its_details_follow_the_file(movies_env: Env) -> None:
    write_photo(movies_env.files / "trip/a.jpg", "2001:02:03 04:05:06")
    movies_env.scan()
    write_photo(movies_env.files / "trip/a.jpg", "1999:01:01 00:00:00")
    set_modified(movies_env.files / "trip/a.jpg", datetime(2030, 1, 1, tzinfo=UTC))
    movies_env.scan()
    assert movies_env.titles(movies.Picture) == ["a.jpg"]
    assert movies_env.fields(movies.Picture, "a.jpg")["date"] == local(datetime(1999, 1, 1))


def test_a_picture_that_becomes_a_poster_is_no_longer_a_picture(movies_env: Env) -> None:
    files = movies_env.files
    video(files / "Heat (1995).mkv")
    write_image(files / "Heat (1995)-poster.jpg")
    write_image(files / "heat.jpg")
    movies_env.scan()
    assert movies_env.titles(movies.Picture) == ["heat.jpg"]  # named after no video
    os.replace(files / "heat.jpg", files / "Heat (1995)-banner.jpg")
    movies_env.scan()
    assert movies_env.titles(movies.Picture) == []


def test_renders_beside_writing_are_pictures(books_env: Env) -> None:
    files = books_env.files
    (files / "Story X").mkdir()
    (files / "Story X/story.md").write_text("# Story X\n\nOnce.\n", encoding="utf-8")
    write_image(files / "Story X/renders/hero.png", (40, 30))
    write_image(files / "Story X/renders/ship.tif", (40, 30))
    write_zip(files / "Story X/sketches.zip", [("s.png", (2015, 3, 4, 0, 0, 0))])
    write_zip(files / "Comics/Saga 001.cbz", [("01.jpg", (2015, 3, 4, 0, 0, 0))])  # a comic
    write_image(files / "Calibre/Author/Book (1)/cover.jpg")  # Calibre's copy of a cover
    (files / "Calibre/Author/Book (1)/metadata.opf").write_text("<package/>", encoding="utf-8")
    write_image(files / "Elsewhere/map.jpg")
    books_env.scan()

    assert books_env.titles(books.Book) == ["Story X"]
    assert books_env.titles(books.Comic) == ["Saga #1"]
    assert books_env.titles(books.Picture) == ["hero.png", "map.jpg", "ship.tif"]
    assert books_env.titles(books.Archive) == ["sketches.zip"]
    hero = books_env.fields(books.Picture, "hero.png")
    assert (hero["dimensions"], hero["folder"]) == ("40 \u00d7 30", "Story X/renders")
    assert books_env.fields(books.Archive, "sketches.zip")["date"] == local(datetime(2015, 3, 4))


def test_a_cover_named_by_front_matter_is_still_a_picture(books_env: Env) -> None:
    files = books_env.files
    (files / "Tale").mkdir()
    (files / "Tale/tale.md").write_text("---\ncover: art.png\n---\n# Tale\n", encoding="utf-8")
    write_image(files / "Tale/art.png")
    books_env.scan()
    assert books_env.files_of("Tale")["cover"] == ["Tale/art.png"]
    assert books_env.titles(books.Picture) == ["art.png"]


def test_an_edited_date_is_kept(movies_env: Env) -> None:
    write_photo(movies_env.files / "a.jpg", "2001:02:03 04:05:06")
    movies_env.scan()
    with movies_env.reader.connect() as conn:
        item = conn.scalar(select(Entity.id).where(Entity.title == "a.jpg"))
    assert item is not None
    when = datetime(1990, 1, 1, tzinfo=UTC)
    movies_env.writer.run(lambda conn: edit_field(conn, movies_env.schema, item, "date", when))
    write_photo(movies_env.files / "a.jpg", "1999:01:01 00:00:00")
    set_modified(movies_env.files / "a.jpg", datetime(2030, 1, 1, tzinfo=UTC))
    movies_env.scan()
    assert movies_env.fields(movies.Picture, "a.jpg")["date"] == when
