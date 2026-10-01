"""Linking files to items by hand, and how scans treat those links (#243)."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from tagalot.core.actions import restore_action
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.links import LinkError, accepting_roles, link_files, unlink_file
from tagalot.core.models import Entity, EntityResource, Resource
from tagalot.core.scanjob import scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.triage import unlinked_files
from tagalot.core.writer import DbWriter
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import EntityRef, IngestContext, Kind, ResourceInfo, Theme, role
from tests.core.media_files import write_image

T0 = datetime(2026, 10, 1, tzinfo=UTC)


class Item(ThemeEntity):
    roles = [
        role("file", kinds={"any"}, many=True, primary=True),
        role("cover", kinds={"image"}),
    ]


class Shelf(Theme):
    """Each .txt file is an item. A picture named ``cover-<name>.png`` is linked as that
    item's cover; any other picture the theme reads into whatever item it's linked to (a
    theme reading a file, as music does with tags), so hand-made links show if they leak."""

    id, name, version = "shelf", "Shelf", 1
    extensions = frozenset({".txt", ".png"})
    entities = [Item]

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            name = resource.relpath.rpartition("/")[2]
            stem = name.rpartition(".")[0]
            if resource.ext == ".txt":
                ctx.link(ctx.upsert(Item, stem, title=stem), resource, "file")
            elif stem.startswith("cover-"):
                ctx.link(ctx.upsert(Item, stem[6:], title=stem[6:]), resource, "cover")
            else:
                for item in ctx.entities_of(resource):
                    ctx.update(item, title=f"renamed by {name}")


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path

    def scan(self, hours: int = 0) -> None:
        root = RootConfig("r", "Shelf", str(self.files))
        when = T0 + timedelta(hours=hours)
        scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=Shelf, schema=self.schema
        )

    def id(self, model: type, title: str) -> int:
        column = Entity.title if model is Entity else Resource.relpath
        with self.reader.connect() as conn:
            found = conn.scalar(select(model.id).where(column == title))  # type: ignore[attr-defined]
        assert found is not None, title
        return int(found)

    def links(self, title: str) -> set[tuple[str, str, bool]]:
        with self.reader.connect() as conn:
            rows = conn.execute(
                select(Resource.relpath, EntityResource.role, EntityResource.by_user)
                .join(EntityResource, EntityResource.resource_id == Resource.id)
                .where(EntityResource.entity_id == self.id(Entity, title))
            )
            return {(r, role, mine) for r, role, mine in rows}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    files = tmp_path / "files"
    files.mkdir()
    (files / "a.txt").write_text("a", encoding="utf-8")
    (files / "b.txt").write_text("b", encoding="utf-8")
    write_image(files / "photo.png", (20, 20))
    write_image(files / "other.png", (30, 30))
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("shelf", 1)))
    schema = open_theme(engine, keep, Shelf).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        result = Env(writer, reader, schema, files)
        result.scan()
        yield result
    reader.dispose()


def _link(env: Env, title: str, files: list[str], role: str) -> object:
    item, ids = env.id(Entity, title), [env.id(Resource, f) for f in files]
    return env.writer.run(lambda conn: link_files(conn, env.schema, item, ids, role))


def test_roles_that_take_a_file() -> None:
    assert [r.name for r in accepting_roles(Item, [Kind.IMAGE])] == ["file", "cover"]
    assert [r.name for r in accepting_roles(Item, [Kind.AUDIO])] == ["file"]
    assert [r.name for r in accepting_roles(Item, [Kind.IMAGE, None])] == ["file"]


def test_a_hand_made_link_and_its_undo(env: Env) -> None:
    with env.reader.connect() as conn:
        assert [f.relpath for f in unlinked_files(conn)] == ["other.png", "photo.png"]
    change = _link(env, "a", ["photo.png"], "cover")
    assert change.label == "Link 1 file to a"  # type: ignore[attr-defined]
    assert ("photo.png", "cover", True) in env.links("a")
    with env.reader.connect() as conn:
        assert [f.relpath for f in unlinked_files(conn)] == ["other.png"]  # not unlinked now
    env.writer.run(lambda conn: restore_action(conn, env.schema, change, forward=False))  # type: ignore[arg-type]
    assert env.links("a") == {("a.txt", "file", False)}
    env.writer.run(lambda conn: restore_action(conn, env.schema, change, forward=True))  # type: ignore[arg-type]
    assert ("photo.png", "cover", True) in env.links("a")


def test_the_theme_never_reads_a_hand_linked_file_into_the_item(env: Env) -> None:
    _link(env, "a", ["photo.png"], "file")
    write_image(env.files / "photo.png", (40, 40), "blue")  # changed: the theme reads it again
    env.scan(1)
    with env.reader.connect() as conn:
        titles = set(conn.scalars(select(Entity.title)))
    assert "a" in titles  # not "renamed by photo.png"
    assert ("photo.png", "file", True) in env.links("a")


def test_the_users_cover_wins_a_one_file_role(env: Env) -> None:
    _link(env, "a", ["photo.png"], "cover")
    write_image(env.files / "cover-a.png", (10, 10))  # the theme would link this as a's cover
    env.scan(1)
    covers = {(f, mine) for f, r, mine in env.links("a") if r == "cover"}
    assert covers == {("photo.png", True)}
    item, photo = env.id(Entity, "a"), env.id(Resource, "photo.png")
    env.writer.run(lambda conn: unlink_file(conn, env.schema, item, photo, "cover"))
    write_image(env.files / "cover-a.png", (12, 12))  # read again: now it may take the role
    env.scan(2)
    covers = {(f, mine) for f, r, mine in env.links("a") if r == "cover"}
    assert covers == {("cover-a.png", False)}


def test_themes_cant_unlink_the_users_links(env: Env) -> None:
    _link(env, "a", ["photo.png"], "file")
    item, photo = env.id(Entity, "a"), env.id(Resource, "photo.png")

    def unlink(conn: object) -> None:
        IngestSession(conn, env.schema).unlink(EntityRef(item, "shelf.item"), photo, "file")  # type: ignore[arg-type]

    env.writer.run(unlink)
    assert ("photo.png", "file", True) in env.links("a")


def test_links_that_cant_be_made(env: Env) -> None:
    with pytest.raises(LinkError, match="has no 'poster' role"):
        _link(env, "a", ["photo.png"], "poster")
    with pytest.raises(LinkError, match="holds one file"):
        _link(env, "a", ["photo.png", "other.png"], "cover")
    with pytest.raises(LinkError, match="doesn't take that kind"):
        _link(env, "a", ["b.txt"], "cover")
    item, text = env.id(Entity, "a"), env.id(Resource, "a.txt")
    with pytest.raises(LinkError, match="Only links you made"):
        env.writer.run(lambda conn: unlink_file(conn, env.schema, item, text, "file"))
