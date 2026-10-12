"""Title cards for items without a picture (#415): the provider, the resolver, the colors."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import Engine, select

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity
from tagalot.core.scanjob import scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.thumbnails.cache import ThumbCache
from tagalot.core.thumbnails.resolve import ThumbnailResolver, ThumbnailResult
from tagalot.core.thumbnails.title_card import card_colors, title_card
from tagalot.core.writer import DbWriter
from tagalot.themes.api import (
    Entity as ThemeEntity,
)
from tagalot.themes.api import (
    Icon,
    IngestContext,
    ResourceInfo,
    RoleImage,
    Theme,
    ThumbnailProvider,
    TitleCard,
    field,
    role,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)


class Book(ThemeEntity):
    authors: str | None = field("Author")
    roles = [
        role("file", kinds={"any"}, primary=True),
        role("cover", kinds={"image"}, thumbnail=True),
    ]


class Shelf(ThemeEntity):
    """A book's folder; its title is empty when the book is at the top."""

    roles = [role("folder", kinds={"dir"}, primary=True)]


class CardsTheme(Theme):
    """``.txt`` files are books by the author in their first line; ``<name>.jpg`` is the
    cover of book ``<name>``; folders are shelves."""

    id, name = "cards", "Cards"
    dirs = True
    entities = [Book, Shelf]

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            stem = resource.relpath.rpartition("/")[2].rpartition(".")[0]
            if resource.kind == "dir":
                ctx.link(ctx.upsert(Shelf, resource.relpath, title=" "), resource, "folder")
            elif resource.ext == ".txt":
                author = Path(resource.path).read_text(encoding="utf-8").strip() or None
                book = ctx.upsert(Book, stem, title=stem, authors=author)
                ctx.link(book, resource, "file")
            elif resource.ext == ".jpg":
                ctx.link(ctx.upsert(Book, stem, title=stem), resource, "cover")

    def thumbnail_chain(self, entity_type: type[ThemeEntity]) -> Sequence[ThumbnailProvider]:
        if entity_type is Book:
            return [RoleImage("cover"), TitleCard("authors"), Icon("file")]
        return [TitleCard(aspect=1), Icon("dir")]


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path
    cache: ThumbCache

    def scan(self) -> None:
        root = RootConfig("r", "Files", str(self.files))
        scan_root(
            self.writer, self.reader, root, root.path, when=T0, theme=CardsTheme,
            schema=self.schema,
        )  # fmt: skip

    def resolve(self, title: str, schema: bool = True) -> ThumbnailResult:
        with self.reader.connect() as conn:
            found = conn.scalar(select(Entity.id).where(Entity.title == title))
        assert found is not None, title
        resolver = ThumbnailResolver(
            self.reader, self.writer, CardsTheme, self.cache, lambda _: str(self.files), 64,
            self.schema if schema else None,
        )  # fmt: skip
        return resolver.resolve(found)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "A", ThemeRef("cards", 1)))
    schema = open_theme(engine, keep, CardsTheme).schema
    files = tmp_path / "files"
    (files / "Shelf").mkdir(parents=True)
    (files / "Dune.txt").write_text("Frank Herbert", encoding="utf-8")
    (files / "Shelf/Emma.txt").write_text("", encoding="utf-8")
    (files / "Covered.txt").write_text("Someone", encoding="utf-8")
    Image.new("RGB", (40, 60), "blue").save(files / "Covered.jpg")
    reader = create_keep_engine(keep.db_path, read_only=True)
    cache = ThumbCache(tmp_path / "k" / "thumbs.db")
    with DbWriter(engine) as writer:
        environment = Env(writer, reader, schema, files, cache)
        environment.scan()
        yield environment
    cache.close()
    reader.dispose()


def test_an_item_without_a_picture_gets_a_title_card(env: Env) -> None:
    result = env.resolve("Dune")
    assert (result.thumbnail, result.icon, result.resource_id) == (None, None, None)
    assert result.card == title_card("Dune", "Frank Herbert")
    assert result.card.aspect == pytest.approx(2 / 3)


def test_a_real_picture_wins(env: Env) -> None:
    result = env.resolve("Covered")
    assert result.thumbnail is not None
    assert result.card is None


def test_an_empty_subtitle_field_leaves_the_line_out(env: Env) -> None:
    card = env.resolve("Emma").card
    assert card is not None
    assert (card.title, card.subtitle) == ("Emma", "")


def test_without_the_themes_tables_the_card_has_no_subtitle(env: Env) -> None:
    card = env.resolve("Dune", schema=False).card
    assert card is not None
    assert (card.title, card.subtitle) == ("Dune", "")


def test_an_item_without_a_title_goes_on_to_the_icon(env: Env) -> None:
    result = env.resolve(" ")
    assert (result.card, result.icon) == (None, "dir")


def test_a_subtitle_that_isnt_a_field_is_left_out(env: Env) -> None:
    class Misnamed(CardsTheme):
        def thumbnail_chain(self, entity_type: type[ThemeEntity]) -> Sequence[ThumbnailProvider]:
            return [TitleCard("nonesuch")]

    with env.reader.connect() as conn:
        dune = conn.scalar(select(Entity.id).where(Entity.title == "Dune"))
    assert dune is not None
    resolver = ThumbnailResolver(
        env.reader, env.writer, Misnamed, env.cache, lambda _: str(env.files), 64, env.schema
    )
    assert resolver.resolve(dune).card == title_card("Dune")


# --- the provider and the colors ---


def test_the_provider_states_itself() -> None:
    assert repr(TitleCard()) == "TitleCard()"
    assert repr(TitleCard("authors")) == "TitleCard('authors')"
    assert repr(TitleCard(aspect=1)) == "TitleCard(aspect=1)"
    with pytest.raises(ValueError, match="aspect"):
        TitleCard(aspect=0.1)


def test_colors_come_from_the_title() -> None:
    assert card_colors("Dune") == card_colors(" dune ")  # the same title, the same card
    titles = ["Dune", "Emma", "Ulysses", "Beloved", "Middlemarch", "Persuasion"]
    assert len({card_colors(t)[0] for t in titles}) == len(titles)
    for title in titles:
        background, ink = card_colors(title)
        assert min(background) > 150  # soft
        assert max(ink) < 110  # dark enough to read on it


def test_a_card_trims_its_text() -> None:
    card = title_card("  Dune ", " Frank Herbert ", aspect=1)
    assert (card.title, card.subtitle, card.aspect) == ("Dune", "Frank Herbert", 1)
