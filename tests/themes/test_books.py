"""The books theme (#296): books and comics from EPUBs and comic archives, their people,
series, universes, collections, and sources."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import aliased

from tagalot.builtin_themes.books import (
    Author,
    Book,
    BooksTheme,
    Collection,
    Comic,
    Series,
    Universe,
    book_name,
    comic_name,
    sort_name,
)
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.keywords import keywords_of
from tagalot.core.models import Entity, EntityContains, EntityResource, Resource
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.thumbnails.render import load_epub_cover, renderer_for
from tagalot.core.writer import DbWriter
from tagalot.themes.api import ResourceInfo
from tagalot.themes.loader import validate_theme
from tests.core.book_files import comic_info, jpeg, write_cbz, write_epub

T0 = datetime(2026, 10, 1, tzinfo=UTC)
PRATCHETT = [("Terry Pratchett", "aut")]


def test_the_theme_is_valid() -> None:
    assert validate_theme(BooksTheme) == []


@pytest.mark.parametrize(
    ("relpath", "expected"),
    [
        ("A/Ursula K. Le Guin - A Wizard of Earthsea (1968).epub",
         ("A Wizard of Earthsea", ["Ursula K. Le Guin"], None, None, 1968)),
        ("Discworld 03 - Equal Rites.epub", ("Equal Rites", [], "Discworld", 3.0, None)),
        ("Mort.epub", ("Mort", [], None, None, None)),
        ("The_Hobbit_(1937).epub", ("The Hobbit", [], None, None, 1937)),
    ],
)  # fmt: skip
def test_book_names(relpath: str, expected: tuple[Any, ...]) -> None:
    name = book_name(relpath)
    found = (name["title"], name["writers"], name["series"], name["series_index"], name["year"])
    assert found == expected


@pytest.mark.parametrize(
    ("relpath", "expected"),
    [
        ("Saga #012 (2013).cbz", ("Saga", "12", None, 2013)),
        ("Saga v2 001.cbr", ("Saga", "1", 2, None)),
        ("The Sandman 1.5.cb7", ("The Sandman", "1.5", None, None)),
        ("Oneshot.cbz", (None, None, None, None)),
    ],
)
def test_comic_names(relpath: str, expected: tuple[Any, ...]) -> None:
    name = comic_name(relpath)
    assert (name["series"], name["number"], name["volume"], name["year"]) == expected


def test_sort_names() -> None:
    assert sort_name("Terry Pratchett") == "Pratchett, Terry"
    assert sort_name("Ursula K. Le Guin") == "Guin, Ursula K. Le"  # good enough; editable
    assert sort_name("Pratchett, Terry") == "Pratchett, Terry"
    assert sort_name("Plato") == "Plato"


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path
    options: dict[str, Any]

    def scan(self, when: datetime = T0) -> ScanReport:
        root = RootConfig("r", "Books", str(self.files), options=self.options)
        return scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=BooksTheme,
            schema=self.schema,
        )  # fmt: skip

    def titles(self, entity: type) -> list[str]:
        type_id = BooksTheme.type_id_of(entity)
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

    def fields(self, entity: type, title: str) -> dict[str, Any]:
        table = self.schema.entities[entity].table
        with self.reader.connect() as conn:
            row = conn.execute(
                select(table).join(Entity, Entity.id == table.c.id).where(Entity.title == title)
            ).one()
        return dict(row._mapping)

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

    def credits(self, relationship: str, title: str) -> list[str]:
        table = self.schema.relationships[relationship].table
        person = aliased(Entity)
        with self.reader.connect() as conn:
            return sorted(
                conn.scalars(
                    select(person.title)
                    .join(table, table.c.a_id == person.id)
                    .join(Entity, Entity.id == table.c.b_id)
                    .where(Entity.title == title)
                )
            )

    def keywords(self, title: str) -> list[str]:
        with self.reader.connect() as conn:
            entity_id = conn.scalar(select(Entity.id).where(Entity.title == title))
            assert entity_id is not None
            return sorted(keywords_of(conn, [entity_id]).get(entity_id, []))


def library(files: Path) -> None:
    write_epub(
        files / "Kobo/colour.epub",
        title="The Colour of Magic",
        creators=PRATCHETT,
        subjects=["Fantasy", "Humor"],
        series=("Discworld", 1),
        cover=jpeg(),
    )
    write_epub(  # the same book from another store: one work, two files
        files / "Humble Bundle/The Colour of Magic.epub",
        title="The Colour of Magic",
        creators=[("Terry Pratchett", None), ("Josh Kirby", "ill")],
        series=("Discworld", 1),
    )
    write_epub(
        files / "Kobo/Equal Rites.epub", title="Equal Rites", creators=PRATCHETT,
        series=("Discworld", 3),
    )  # fmt: skip
    write_epub(files / "Loose/Discworld 02 - The Light Fantastic.epub")  # its name only
    write_epub(files / "Loose/Ursula K. Le Guin - A Wizard of Earthsea (1968).epub")
    write_epub(
        files / "Humble Bundle/Mistborn.epub",
        title="Mistborn",
        creators=[("Brandon Sanderson", None)],
        epub3_series=("Mistborn", 1),
        sets=["The Mistborn Trilogy"],
    )
    write_cbz(
        files / "Comixology/Saga 001.cbz",
        comic_info(
            Series="Saga", Number="1", Writer="Brian K. Vaughan", Penciller="Fiona Staples",
            Genre="Science Fiction", Web="https://comics.example.com/saga-1",
        ),
    )  # fmt: skip
    write_cbz(
        files / "Comixology/Ms Marvel 1.cbz",
        comic_info(
            Series="Ms. Marvel", Number="1", Title="No Normal", Writer="G. Willow Wilson",
            SeriesGroup="Marvel", Year="2014",
        ),
    )  # fmt: skip
    write_cbz(files / "Loose/Sandman #001 (1989).cbz")  # no ComicInfo


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    files = tmp_path / "files"
    library(files)
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "B", ThemeRef("books", 1)))
    schema = open_theme(engine, keep, BooksTheme).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, files, {"source_level": 1})
    reader.dispose()
    engine.dispose()


def test_a_scan_makes_works_people_and_series(env: Env) -> None:
    env.scan()
    assert env.titles(Book) == [
        "A Wizard of Earthsea",
        "Equal Rites",
        "Mistborn",
        "The Colour of Magic",
        "The Light Fantastic",
    ]
    assert env.titles(Comic) == ["No Normal", "Saga #1", "Sandman #1"]
    assert env.files_of("The Colour of Magic") == [
        "Humble Bundle/The Colour of Magic.epub",
        "Kobo/colour.epub",
    ]
    assert env.titles(Author) == [
        "Brandon Sanderson",
        "Brian K. Vaughan",
        "Fiona Staples",
        "G. Willow Wilson",
        "Josh Kirby",
        "Terry Pratchett",
        "Ursula K. Le Guin",
    ]
    assert env.titles(Series) == ["Discworld", "Mistborn", "Ms. Marvel", "Saga", "Sandman"]
    assert env.titles(Universe) == ["Marvel"]
    assert env.titles(Collection) == ["The Mistborn Trilogy"]
    contents = env.contents()
    assert contents["Discworld"] == ["Equal Rites", "The Colour of Magic", "The Light Fantastic"]
    assert contents["Marvel"] == ["Ms. Marvel"]
    assert contents["Ms. Marvel"] == ["No Normal"]
    assert contents["The Mistborn Trilogy"] == ["Mistborn"]


def test_fields_credits_and_sources(env: Env) -> None:
    env.scan()
    colour = env.fields(Book, "The Colour of Magic")
    assert colour["authors"] == "Terry Pratchett"
    assert (colour["series"], colour["series_index"]) == ("Discworld", 1.0)
    assert set(colour["sources"].split(", ")) == {"Humble Bundle", "Kobo"}
    assert env.credits("writers", "The Colour of Magic") == ["Terry Pratchett"]
    assert env.credits("artists", "The Colour of Magic") == ["Josh Kirby"]
    earthsea = env.fields(Book, "A Wizard of Earthsea")
    assert (earthsea["authors"], earthsea["year"]) == ("Ursula K. Le Guin", 1968)
    assert env.fields(Book, "The Light Fantastic")["series_index"] == 2.0
    saga = env.fields(Comic, "Saga #1")
    assert (saga["writers"], saga["artists"]) == ("Brian K. Vaughan", "Fiona Staples")
    assert (saga["number"], saga["series_index"]) == ("1", 1.0)
    assert saga["link"] == "https://comics.example.com/saga-1"
    assert saga["sources"] == "Comixology"
    assert env.credits("comic_writers", "Saga #1") == ["Brian K. Vaughan"]
    assert env.credits("comic_artists", "Saga #1") == ["Fiona Staples"]
    assert env.fields(Comic, "No Normal")["universe"] == "Marvel"
    assert env.fields(Series, "Ms. Marvel")["universe"] == "Marvel"
    assert env.fields(Author, "Terry Pratchett")["sort_name"] == "Pratchett, Terry"


def test_sources_from_the_files_without_a_source_level(env: Env) -> None:
    env.options = {}
    env.scan()
    assert env.fields(Comic, "Saga #1")["sources"] == "comics.example.com"
    assert env.fields(Book, "Mistborn")["sources"] is None


def test_keywords_are_reported(env: Env) -> None:
    env.scan()
    assert env.keywords("The Colour of Magic") == ["Fantasy", "Humor"]
    assert env.keywords("Saga #1") == ["Science Fiction"]


def test_a_changed_file_updates_its_work_in_place(env: Env) -> None:
    env.scan()
    write_epub(
        env.files / "Kobo/Equal Rites.epub",
        title="Equal Rites",
        creators=[("Terence Pratchett", None)],
        series=("The Witches", 1),
    )
    env.scan(T0 + timedelta(minutes=1))
    rites = env.fields(Book, "Equal Rites")
    assert (rites["authors"], rites["series"]) == ("Terence Pratchett", "The Witches")
    assert env.credits("writers", "Equal Rites") == ["Terence Pratchett"]
    contents = env.contents()
    assert "Equal Rites" not in contents["Discworld"]
    assert contents["The Witches"] == ["Equal Rites"]
    assert "Terry Pratchett" in env.titles(Author)  # still credited on The Colour of Magic


def test_people_and_series_left_with_nothing_are_gone(env: Env) -> None:
    env.scan()
    write_cbz(
        env.files / "Comixology/Ms Marvel 1.cbz",
        comic_info(Series="Kamala Khan", Number="1", Title="No Normal", Writer="Someone"),
    )
    env.scan(T0 + timedelta(minutes=1))
    assert "G. Willow Wilson" not in env.titles(Author)
    assert "Ms. Marvel" not in env.titles(Series)
    assert env.titles(Universe) == []  # its only series went with it


def test_a_work_with_several_files_gains_from_each(env: Env) -> None:
    env.scan()
    write_epub(  # one of its two files stops naming the illustrator
        env.files / "Humble Bundle/The Colour of Magic.epub",
        title="The Colour of Magic",
        creators=[("Terry Pratchett", None)],
        series=("Discworld", 1),
    )
    env.scan(T0 + timedelta(minutes=1))
    assert env.credits("artists", "The Colour of Magic") == ["Josh Kirby"]  # the other file


def test_an_unreadable_file_is_still_a_work(env: Env) -> None:
    (env.files / "Loose/Broken Book.epub").write_bytes(b"not a zip")
    env.scan()
    assert "Broken Book" in env.titles(Book)


def _record(title: str, **fields: Any) -> Any:
    return SimpleNamespace(title=title, fields=fields)


def test_near_duplicates() -> None:
    theme = BooksTheme()
    a = _record("The Colour of Magic", authors="Terry Pratchett", series_index=None)
    b = _record("The Color of Magic", authors="Terry Pratchett", series_index=None)
    assert list(theme.blocking_keys(Book, a)) == ["terry pratchett"]
    assert theme.similarity(Book, a, b) >= theme.near_duplicate_threshold
    one = _record("Mistborn 1", authors="B", series_index=1.0)
    two = _record("Mistborn 2", authors="B", series_index=2.0)
    assert theme.similarity(Book, one, two) == 0.0
    assert list(theme.blocking_keys(Author, _record("x"))) == []
    assert list(theme.blocking_keys(Comic, _record("x", writers="Wilson, Other"))) == ["wilson"]


def _resource(path: Path) -> ResourceInfo:
    return ResourceInfo(1, "r", path.name, "file", path.suffix, 1, 1, str(path))


def test_an_epub_shows_its_cover(tmp_path: Path) -> None:
    named = write_epub(tmp_path / "a.epub", title="A", cover=jpeg((10, 200, 10)))
    renderer = renderer_for(_resource(named))
    assert renderer is not None
    assert renderer.id == "epub_cover"
    image = load_epub_cover(str(named), 64)
    assert image is not None
    assert image.getpixel((5, 5))[1] > 150  # type: ignore[index]
    unnamed = write_epub(tmp_path / "b.epub", title="B")
    assert load_epub_cover(str(unnamed), 64) is None  # no images at all


def test_a_comic_shows_its_first_page(tmp_path: Path) -> None:
    renderer = renderer_for(_resource(write_cbz(tmp_path / "x.cbz")))
    assert renderer is not None
    assert renderer.id == "archive_image"
