"""The assets2d theme: artists and their images, fonts, and archives (#81)."""

import os
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select
from sqlalchemy.orm import aliased

from tagalot.builtin_themes.assets2d import (
    Archive,
    Artist,
    Assets2DTheme,
    Font,
    Image,
    artist_of,
    is_image_name,
)
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.detail import load_preview
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, EntityContains, EntityTag, Resource, Tag
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.search import run_search
from tagalot.core.search_fields import search_fields, view_spec
from tagalot.core.tags import TagTree, TagTreeCache
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.thumbnails.cache import ThumbCache
from tagalot.core.thumbnails.resolve import ThumbnailResolver
from tagalot.core.writer import DbWriter
from tagalot.themes.api import SearchView
from tagalot.themes.loader import validate_theme
from tests.core.media_files import png_bytes, write_font, write_image

T0 = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path
    cache: ThumbCache

    def scan(
        self,
        when: datetime = T0,
        keep_options: dict[str, Any] | None = None,
        root_options: dict[str, Any] | None = None,
    ) -> ScanReport:
        root = RootConfig("r", "Assets", str(self.files), options=root_options or {})
        return scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=Assets2DTheme,
            schema=self.schema, theme_options=keep_options,
        )  # fmt: skip

    def titles(self, entity: type) -> list[str]:
        type_id = Assets2DTheme.type_id_of(entity)
        with self.reader.connect() as conn:
            return sorted(conn.scalars(select(Entity.title).where(Entity.type == type_id)))

    def fields(self, entity: type, title: str) -> dict[str, Any]:
        table = self.schema.entities[entity].table
        with self.reader.connect() as conn:
            row = conn.execute(
                select(table).join(Entity, Entity.id == table.c.id).where(Entity.title == title)
            ).one()
        return dict(row._mapping)

    def artists(self) -> dict[str, list[str]]:
        """``{artist: [asset titles]}`` from the containment edges."""
        parent, child = aliased(Entity), aliased(Entity)
        with self.reader.connect() as conn:
            rows = conn.execute(
                select(parent.title, child.title)
                .join(EntityContains, EntityContains.parent_id == parent.id)
                .join(child, child.id == EntityContains.child_id)
            ).all()
        found: dict[str, list[str]] = {}
        for artist, asset in rows:
            found.setdefault(artist, []).append(asset)
        return {a: sorted(v) for a, v in sorted(found.items())}

    def entity(self, title: str) -> int:
        with self.reader.connect() as conn:
            found = conn.scalar(select(Entity.id).where(Entity.title == title))
        assert found is not None, title
        return found


@pytest.fixture
def library(tmp_path: Path) -> Path:
    files = tmp_path / "files"
    write_image(files / "Aurora/sky.png", (300, 200))
    write_image(files / "Aurora/folder.jpg", (64, 64), "purple")
    write_font(files / "Aurora/fonts/Aileron-Regular.ttf")
    with zipfile.ZipFile(files / "Aurora/pack.zip", "w") as zf:
        zf.writestr("pack/cover.png", png_bytes((40, 20)))
        zf.writestr("pack/a.png", png_bytes())
        zf.writestr("pack/b.PNG", png_bytes())
        zf.writestr("pack/readme.txt", b"hi")
        zf.writestr("__MACOSX/pack/._cover.png", b"meta")
    write_image(files / "Kenji Sato/sketch.jpg", (120, 90))
    write_image(files / "loose.png", (10, 10))
    (files / "Kenji Sato/broken.png").write_bytes(b"not an image")
    (files / "Kenji Sato/notes.txt").write_text("not an asset", encoding="utf-8")
    return files


@pytest.fixture
def env(tmp_path: Path, library: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "A", ThemeRef("assets2d", 1)))
    schema = open_theme(engine, keep, Assets2DTheme).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    cache = ThumbCache(tmp_path / "k" / "thumbs.db")
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, library, cache)
    cache.close()
    reader.dispose()


def test_the_theme_is_valid() -> None:
    assert validate_theme(Assets2DTheme) == []
    assert (Assets2DTheme.thumbnail_max, Assets2DTheme.thumbnail_default) == (1024, 256)


def test_a_scan_makes_artists_and_assets_by_kind(env: Env) -> None:
    report = env.scan()
    assert report.ingest_errors == []
    assert env.titles(Artist) == ["Aurora", "Kenji Sato"]
    assert env.titles(Image) == ["broken.png", "folder.jpg", "loose.png", "sketch.jpg", "sky.png"]
    assert env.titles(Font) == ["Aileron-Regular.ttf"]
    assert env.titles(Archive) == ["pack.zip"]
    assert env.artists() == {
        "Aurora": ["Aileron-Regular.ttf", "folder.jpg", "pack.zip", "sky.png"],
        "Kenji Sato": ["broken.png", "sketch.jpg"],
    }  # loose.png, directly in the root, has no artist


def test_details_are_read_for_each_kind(env: Env) -> None:
    env.scan()
    sky = env.fields(Image, "sky.png")
    assert (sky["width"], sky["height"], sky["dimensions"]) == (300, 200, "300 \u00d7 200")
    assert (sky["artist"], sky["extension"], sky["folder"]) == ("Aurora", ".png", "Aurora")
    font = env.fields(Font, "Aileron-Regular.ttf")
    assert (font["family"], font["style"], font["folder"]) == ("Aileron", "Regular", "Aurora/fonts")
    assert env.fields(Archive, "pack.zip")["images"] == 3  # not the text or macOS metadata
    assert env.fields(Image, "loose.png")["artist"] is None


def test_an_unreadable_file_is_still_an_asset_with_a_warning(env: Env) -> None:
    report = env.scan()
    broken = env.fields(Image, "broken.png")
    assert broken["width"] is None
    assert [(w.relpath, w.message.split(":")[0]) for w in report.ingest_warnings] == [
        ("Kenji Sato/broken.png", "Couldn't read its details")
    ]


def test_moving_a_file_to_another_artist(env: Env) -> None:
    env.scan()
    sky = env.entity("sky.png")
    os.replace(env.files / "Aurora/sky.png", env.files / "Kenji Sato/sky.png")
    report = env.scan(T0 + timedelta(hours=1))
    assert len(report.moves) == 1
    assert env.entity("sky.png") == sky  # the same asset, so its tags stay
    artists = env.artists()
    assert "sky.png" in artists["Kenji Sato"]
    assert "sky.png" not in artists["Aurora"]
    assert env.fields(Image, "sky.png")["artist"] == "Kenji Sato"


def test_thumbnails_for_every_kind(env: Env) -> None:
    env.scan()
    resolver = ThumbnailResolver(
        env.reader, env.writer, Assets2DTheme, env.cache, lambda _: str(env.files), 64
    )
    results = {
        title: resolver.resolve(env.entity(title))
        for title in ["sky.png", "Aileron-Regular.ttf", "pack.zip", "Aurora", "Kenji Sato"]
    }
    for title, result in results.items():
        assert result.thumbnail is not None, title
    assert (results["pack.zip"].thumbnail.width, results["pack.zip"].thumbnail.height) == (40, 20)  # type: ignore[union-attr]
    assert (results["Aileron-Regular.ttf"].thumbnail.width) == 64  # type: ignore[union-attr]
    # Aurora has a folder image; Kenji Sato shows one of its assets.
    assert results["Aurora"].resource_id == results_resource(env, "Aurora/folder.jpg")
    assert results["Kenji Sato"].resource_id == results_resource(env, "Kenji Sato/sketch.jpg")


def results_resource(env: Env, relpath: str) -> int:
    with env.reader.connect() as conn:
        found = conn.scalar(select(Resource.id).where(Resource.relpath == relpath))
    assert found is not None
    return found


def test_changing_the_artist_level_reingests_the_root(env: Env) -> None:
    env.scan()
    sky = env.entity("sky.png")
    assert env.titles(Artist) == ["Aurora", "Kenji Sato"]

    # Level 2 (set on the keep): only Aurora/fonts is that deep.
    report = env.scan(T0 + timedelta(hours=1), keep_options={"artist_level": 2})
    assert report.ingested == 10  # all 7 files and 3 folders again
    assert env.titles(Artist) == ["fonts"]  # Aurora and Kenji Sato were left empty
    assert env.artists() == {"fonts": ["Aileron-Regular.ttf"]}
    assert env.fields(Image, "sky.png")["artist"] is None
    assert env.entity("sky.png") == sky  # assets are kept, with their tags

    # The same values again: nothing to redo.
    again = env.scan(T0 + timedelta(hours=2), keep_options={"artist_level": 2})
    assert again.ingested == 0

    # A root's own value wins over the keep's: back to level 1.
    back = env.scan(
        T0 + timedelta(hours=3), keep_options={"artist_level": 2}, root_options={"artist_level": 1}
    )
    assert back.ingested == 10
    assert env.titles(Artist) == ["Aurora", "Kenji Sato"]
    assert "sky.png" in env.artists()["Aurora"]


def test_a_bad_option_value_is_reported_and_the_default_used(env: Env) -> None:
    report = env.scan(keep_options={"artist_level": "two"})
    assert env.titles(Artist) == ["Aurora", "Kenji Sato"]
    assert any("artist_level must be a whole number" in w.message for w in report.ingest_warnings)


@pytest.mark.parametrize(
    ("relpath", "artist"),
    [("Aurora/sky.png", "Aurora"), ("Aurora/a/b/c.png", "Aurora"), ("sky.png", None)],
)
def test_artist_of(relpath: str, artist: str | None) -> None:
    assert artist_of(relpath) == artist


@pytest.mark.parametrize(
    ("name", "image"),
    [
        ("a.png", True),
        ("dir/B.JPG", True),
        ("x.psd", True),
        ("readme.txt", False),
        (".hidden.png", False),
        ("__MACOSX/._a.png", False),
    ],
)
def test_is_image_name(name: str, image: bool) -> None:
    assert is_image_name(name) is image


# --- views (#83) ---


def _view(env: Env, name: str, tree: TagTree, include: tuple[int, ...] = ()) -> list[str]:
    view = next(v for v in Assets2DTheme.views if isinstance(v, SearchView) and v.name == name)
    spec = replace(view_spec(env.schema, view), include=include)
    with env.reader.connect() as conn:
        hits = run_search(conn, spec, tree, fields=search_fields(env.schema, spec.types))
    return [h.title for h in hits]


def test_each_view_lists_its_kind(env: Env) -> None:
    env.scan()
    tree = TagTreeCache(env.reader).get()
    names = [v.name for v in Assets2DTheme.views if isinstance(v, SearchView)]
    assert names == ["Assets", "Artists", "Images", "Fonts", "Archives"]
    assert len(_view(env, "Assets", tree)) == 7
    assert _view(env, "Artists", tree) == ["Aurora", "Kenji Sato"]
    assert _view(env, "Images", tree) == [
        "broken.png",
        "folder.jpg",
        "loose.png",
        "sketch.jpg",
        "sky.png",
    ]
    assert _view(env, "Fonts", tree) == ["Aileron-Regular.ttf"]
    assert _view(env, "Archives", tree) == ["pack.zip"]


def test_an_artists_tags_count_for_its_assets(env: Env) -> None:
    env.scan()
    kenji = env.entity("Kenji Sato")
    env.writer.run(lambda conn: conn.execute(insert(Tag).values(id=1, name="Sketchy")))
    env.writer.run(lambda conn: conn.execute(insert(EntityTag).values(entity_id=kenji, tag_id=1)))
    tree = TagTreeCache(env.reader).get()
    assert _view(env, "Images", tree, include=(1,)) == ["broken.png", "sketch.jpg"]
    assert _view(env, "Artists", tree, include=(1,)) == ["Kenji Sato"]
    assert _view(env, "Fonts", tree, include=(1,)) == []


# --- the preview strip (#90) ---


def test_preview_of_an_asset(env: Env) -> None:
    env.scan()
    with env.reader.connect() as conn:
        preview = load_preview(conn, env.schema, env.entity("sky.png"))
        missing = load_preview(conn, env.schema, 999_999)
    assert preview is not None
    assert (preview.title, preview.type_label) == ("sky.png", "Image")
    facts = {f.label: f.value for f in preview.facts}
    assert facts["Artist"] == "Aurora"
    assert facts["Extension"] == ".png"
    assert facts["Dimensions"] == "300 \u00d7 200"
    assert "Width" not in facts  # not a card field
    assert preview.file == "Assets \u203a Aurora/sky.png"
    assert missing is None
