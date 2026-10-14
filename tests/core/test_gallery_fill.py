"""Gallery roles filled from named folders (#387): the pictures and archives under an item's
named subfolders, at any depth, and archives of those names beside them; scans follow them,
and the theme's and the user's own links stay."""

import datetime
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, update

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.entity_state import snapshot_entities
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.links import link_files
from tagalot.core.models import Entity, EntityResource, Resource
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import IngestContext, Theme, role
from tagalot.themes.loader import validate_theme
from tests.core.media_files import png_bytes

PNG = png_bytes((8, 8))


class Film(ThemeEntity):
    roles = [
        role("folder", kinds={"dir"}, primary=True),
        role(
            "shots",
            kinds={"image", "archive"},
            many=True,
            gallery=True,
            folders=("Screenshots", "Screens"),
        ),
        role("art", kinds={"image"}, many=True, gallery=True, folders=("Art",)),
    ]


class Films(Theme):
    id, name, version, api_version = "films", "Films", 1, 14
    dirs = True
    entities = [Film]

    def ingest(self, batch: Any, ctx: IngestContext) -> None:
        for resource in batch:
            if resource.kind == "dir" and "/" not in resource.relpath:
                ctx.link(
                    ctx.upsert(Film, resource.relpath, title=resource.relpath), resource, "folder"
                )
            elif resource.relpath.endswith("/Screens/by theme.png"):  # the theme's own link
                film = ctx.upsert(Film, resource.relpath.partition("/")[0])
                ctx.link(film, resource, "shots")


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path
    clock: int = 0

    def scan(self) -> ScanReport:
        self.clock += 1
        when = datetime.datetime(2026, 10, 13, tzinfo=datetime.UTC) + datetime.timedelta(
            minutes=self.clock
        )
        root = RootConfig("r", "Films", str(self.files))
        return scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=Films, schema=self.schema
        )

    def put(self, *relpaths: str) -> None:
        for relpath in relpaths:
            path = self.files / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PNG)

    def film(self, title: str = "Heat") -> int:
        with self.reader.connect() as conn:
            found = conn.scalar(select(Entity.id).where(Entity.title == title))
        assert found is not None
        return found

    def links(self, role: str = "shots", title: str = "Heat") -> dict[str, tuple[bool, bool]]:
        """The role's files: relpath -> (by the user, from the folders)."""
        with self.reader.connect() as conn:
            rows = conn.execute(
                select(Resource.relpath, EntityResource.by_user, EntityResource.by_folder)
                .join(Resource, Resource.id == EntityResource.resource_id)
                .where(EntityResource.entity_id == self.film(title), EntityResource.role == role)
            ).all()
        return {rel.partition("/")[2]: (user, folder) for rel, user, folder in rows}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    files = tmp_path / "files"
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "F", ThemeRef(Films.id, 1)))
    schema = open_theme(engine, keep, Films).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        environment = Env(writer, reader, schema, files)
        environment.put("Heat/poster.png")
        yield environment
    reader.dispose()
    engine.dispose()


FOLDER = (False, True)


def test_the_named_folders_fill_the_gallery(env: Env) -> None:
    env.put(
        "Heat/Screenshots/a.png",
        "Heat/Screenshots/Disc 2/Deleted/b.png",  # at any depth
        "Heat/SCREENS/c.png",  # case aside
        "Heat/Other/d.png",  # not a named folder
        "Heat/a.png",  # loose in the item's folder
        "Heat/Extras/Screenshots/e.png",  # named, but not directly in the item's folder
        "Heat/Art/f.png",
        "Heat/Art/Inside.zip",  # the art role takes no archives
    )
    (env.files / "Heat/Screenshots/notes.txt").write_text("no", encoding="utf-8")
    (env.files / "Heat/Screens.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # beside them
    (env.files / "Heat/Screenshots/More.7z").write_bytes(b"7z")
    (env.files / "Heat/Other.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    env.scan()
    assert env.links() == {
        "Screenshots/a.png": FOLDER,
        "Screenshots/More.7z": FOLDER,
        "Screenshots/Disc 2/Deleted/b.png": FOLDER,
        "SCREENS/c.png": FOLDER,
        "Screens.zip": FOLDER,
    }
    assert env.links("art") == {"Art/f.png": FOLDER}


def test_scans_follow_pictures_that_come_and_go(env: Env) -> None:
    env.put("Heat/Screenshots/a.png", "Heat/Screenshots/b.png")
    env.scan()
    assert set(env.links()) == {"Screenshots/a.png", "Screenshots/b.png"}
    os.remove(env.files / "Heat/Screenshots/a.png")
    env.put("Heat/Screenshots/c.png")
    env.scan()
    assert set(env.links()) == {"Screenshots/b.png", "Screenshots/c.png"}
    (env.files / "Heat/Screenshots").rename(env.files / "Heat/Stills")
    env.scan()
    assert env.links() == {}


def test_the_themes_and_the_users_links_stay(env: Env) -> None:
    env.put("Heat/Screens/by theme.png", "Heat/Other/mine.png", "Heat/Screens/also mine.png")
    env.scan()
    assert env.links()["Screens/by theme.png"] == (False, False)  # the theme's, not filled
    heat = env.film()
    with env.reader.connect() as conn:
        mine = conn.scalars(
            select(Resource.id).where(
                Resource.relpath.in_(["Heat/Other/mine.png", "Heat/Screens/also mine.png"])
            )
        ).all()
    env.writer.run(lambda conn: link_files(conn, env.schema, heat, list(mine), "shots"))
    assert env.links()["Other/mine.png"] == (True, False)
    assert env.links()["Screens/also mine.png"] == (True, True)  # yours now, still filled
    (env.files / "Heat/Screens").rename(env.files / "Heat/Stills")
    env.scan()
    assert env.links() == {
        "Stills/by theme.png": (False, False),  # moved with its folder: the theme's
        "Other/mine.png": (True, False),
        "Stills/also mine.png": (True, False),  # yours: kept, no longer filled
    }


def test_themes_dont_see_folder_links_and_snapshots_keep_them(env: Env) -> None:
    env.put("Heat/Screenshots/a.png")
    env.scan()
    with env.reader.connect() as conn:
        picture = conn.scalar(
            select(Resource.id).where(Resource.relpath == "Heat/Screenshots/a.png")
        )
    assert picture is not None
    film = env.film()

    def probe(conn: Any) -> tuple[Any, Any]:
        seen = IngestSession(conn, env.schema, {}, {}).entities_of(picture)
        return seen, snapshot_entities(conn, env.schema, [film])[film]

    seen, state = env.writer.run(probe)
    assert seen == []  # the core's link, not the theme's
    assert state is not None
    assert (picture, "shots", 0, False, True) in state.links  # undo puts it back as it was


def test_an_item_at_the_top_of_the_root_and_a_files_folder(tmp_path: Path) -> None:
    class Clip(ThemeEntity):
        roles = [
            role("video", kinds={"video"}, primary=True),
            role("stills", kinds={"image"}, many=True, gallery=True, folders=("Stills",)),
        ]

    class Clips(Theme):
        id, name, api_version = "clips", "Clips", 14
        entities = [Clip]

        def ingest(self, batch: Any, ctx: IngestContext) -> None:
            for r in batch:
                if r.ext == ".mp4":
                    ctx.link(ctx.upsert(Clip, r.relpath, title=r.relpath), r, "video")

    files = tmp_path / "files"
    for rel in ("top.mp4", "Stills/a.png", "Trip/trip.mp4", "Trip/Stills/b.png"):
        (files / rel).parent.mkdir(parents=True, exist_ok=True)
        (files / rel).write_bytes(PNG)
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "C", ThemeRef("clips", 1)))
    schema = open_theme(engine, keep, Clips).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        root = RootConfig("r", "Clips", str(files))
        scan_root(writer, reader, root, root.path, theme=Clips, schema=schema)
        with reader.connect() as conn:
            rows = conn.execute(
                select(Entity.title, Resource.relpath)
                .join(EntityResource, EntityResource.entity_id == Entity.id)
                .join(Resource, Resource.id == EntityResource.resource_id)
                .where(EntityResource.role == "stills")
            ).all()
    reader.dispose()
    engine.dispose()
    # a file's folder: the root's top for top.mp4, Trip for Trip/trip.mp4
    assert sorted(rows) == [("Trip/trip.mp4", "Trip/Stills/b.png"), ("top.mp4", "Stills/a.png")]


def test_the_loader_checks_gallery_roles() -> None:
    class Bad(ThemeEntity):
        roles = [
            role("folder", kinds={"dir"}, primary=True),
            role("one", kinds={"image"}, gallery=True),
            role("docs", kinds={"audio"}, many=True, gallery=True),
            role("plain", kinds={"image"}, many=True, folders=("Shots",)),
            role("paths", kinds={"image"}, many=True, gallery=True, folders=("a/b", " ")),
        ]

    class Old(Theme):
        id, name, api_version = "old", "Old", 13
        entities = [Bad]

    assert validate_theme(Old) == [
        "Bad.one: a gallery role needs many=True",
        "Bad.docs: a gallery role needs the image kind",
        "Bad.plain: folders needs gallery=True",
        "Bad.paths: folders are names, not paths: ['a/b', ' ']",
        "a theme with gallery roles must set api_version = 14 (or later)",
    ]
    assert validate_theme(Films) == []
    assert role("x", kinds={"image"}, many=True, gallery=True, folders="Shots").folders == (
        "Shots",
    )


def test_a_link_already_there_isnt_made_again(env: Env) -> None:
    env.put("Heat/Screenshots/a.png")
    env.scan()
    env.writer.run(lambda conn: conn.execute(update(EntityResource).values(by_folder=False)))
    env.scan()  # nothing changed: the links stay, and aren't duplicated
    assert env.links() == {"Screenshots/a.png": (False, False)}
