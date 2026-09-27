"""Tests for the built-in generic theme."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import Engine, delete, func, insert, select, update

from tagalot.builtin_themes.generic import File, GenericTheme
from tagalot.core.db import open_keep_database
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.models import (
    Entity,
    EntityResource,
    FieldProvenance,
    FieldSource,
    Resource,
    ResourceKind,
    Root,
)
from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tags import TagTree
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import ResourceInfo
from tagalot.themes.loader import load_themes, validate_theme

MTIME = 1_790_000_000_000_000_000  # ns


@dataclass
class Env:
    engine: Engine
    schema: ThemeSchema

    def add(
        self, relpath: str, size: int = 10, kind: Literal["file", "dir"] = "file"
    ) -> ResourceInfo:
        ext = Path(relpath).suffix.lower() if kind == "file" else ""
        with self.engine.begin() as conn:
            rid = conn.execute(
                insert(Resource)
                .values(
                    root_id="r",
                    relpath=relpath,
                    kind=ResourceKind(kind),
                    ext=ext,
                    size=size,
                    mtime_ns=MTIME,
                )
                .returning(Resource.id)
            ).scalar_one()
        return ResourceInfo(
            rid, "r", relpath, kind, ext, size if kind == "file" else None, MTIME, f"/x/{relpath}"
        )

    def ingest(self, *batch: ResourceInfo) -> None:
        with self.engine.begin() as conn:
            ctx = IngestSession(conn, self.schema)
            GenericTheme().ingest(batch, ctx)
            ctx.flush()

    def files(self) -> dict[str, dict[str, object]]:
        table = self.schema.entities[File].table
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    Entity.id, Entity.title, table.c.extension, table.c.folder, table.c.size
                ).join(table, table.c.id == Entity.id)
            )
            return {t: {"id": i, "ext": e, "folder": f, "size": s} for i, t, e, f, s in rows}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "Files", ThemeRef("generic", 1)))
    schema = open_theme(engine, keep, GenericTheme).schema
    with engine.begin() as conn:
        conn.execute(insert(Root).values(id="r", name="R"))
    yield Env(engine, schema)
    engine.dispose()


def test_generic_is_a_valid_builtin_theme() -> None:
    assert validate_theme(GenericTheme) == []
    catalog = load_themes(user_dir=Path("does-not-exist"))
    loaded = catalog.get("generic")
    assert loaded is not None
    assert (loaded.theme, loaded.builtin, catalog.problems) == (GenericTheme, True, [])


def test_one_entity_per_file_titled_by_name(env: Env) -> None:
    env.ingest(
        env.add("Photos/2024/IMG_2031.JPG", size=2048),
        env.add("notes.txt"),
        env.add("Photos", kind="dir"),  # folders are not entities
    )
    files = env.files()
    assert set(files) == {"IMG_2031.JPG", "notes.txt"}
    assert files["IMG_2031.JPG"] | {"id": 0} == {
        "id": 0,
        "ext": ".jpg",
        "folder": "Photos/2024",
        "size": 2048,
    }
    assert files["notes.txt"]["folder"] == ""
    with env.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(EntityResource)) == 2
        modified = conn.scalar(select(env.schema.entities[File].table.c.modified))
    assert modified == datetime.fromtimestamp(MTIME / 1e9, UTC)


def test_reingesting_a_changed_file_updates_its_entity(env: Env) -> None:
    info = env.add("a.txt", size=1)
    env.ingest(info)
    first = env.files()["a.txt"]["id"]
    env.ingest(ResourceInfo(info.id, "r", "a.txt", "file", ".txt", 999, MTIME + 1, info.path))
    assert env.files() == {"a.txt": {"id": first, "ext": ".txt", "folder": "", "size": 999}}


def test_a_moved_file_keeps_its_entity(env: Env) -> None:
    old = env.add("Inbox/song.flac")
    env.ingest(old)
    entity_id = env.files()["song.flac"]["id"]
    # Move detection (#23) gives the entity's links to the new resource and drops the old one.
    new = env.add("Music/Artist/song.flac")
    with env.engine.begin() as conn:
        conn.execute(
            update(EntityResource)
            .where(EntityResource.resource_id == old.id)
            .values(resource_id=new.id)
        )
    env.ingest(new)
    assert env.files() == {
        "song.flac": {"id": entity_id, "ext": ".flac", "folder": "Music/Artist", "size": 10}
    }


def test_a_new_file_at_a_vacated_path_gets_its_own_entity(env: Env) -> None:
    old = env.add("song.flac")
    env.ingest(old)
    moved_id = env.files()["song.flac"]["id"]
    moved = env.add("Archive/song.flac")
    with env.engine.begin() as conn:
        conn.execute(
            update(EntityResource)
            .where(EntityResource.resource_id == old.id)
            .values(resource_id=moved.id)
        )
        conn.execute(delete(Resource).where(Resource.id == old.id))
    env.ingest(moved)
    newcomer = env.add("song.flac", size=77)  # a different file where the old one used to be
    env.ingest(newcomer)
    with env.engine.connect() as conn:
        links = dict(
            conn.execute(select(EntityResource.resource_id, EntityResource.entity_id)).all()
        )
    assert links[moved.id] == moved_id  # the moved file still owns its entity
    assert links[newcomer.id] != moved_id  # the newcomer got a new one


def test_a_user_renamed_title_survives_rescans(env: Env) -> None:
    info = env.add("IMG_0001.jpg")
    env.ingest(info)
    entity_id = env.files()["IMG_0001.jpg"]["id"]
    with env.engine.begin() as conn:
        conn.execute(
            update(Entity).where(Entity.id == entity_id).values(title="Sunset at the lake")
        )
        conn.execute(
            update(FieldProvenance)
            .where(FieldProvenance.entity_id == entity_id, FieldProvenance.field == "title")
            .values(source=FieldSource.USER)
        )
    env.ingest(info)
    assert "Sunset at the lake" in env.files()


def test_files_are_searchable_by_name_and_folder(env: Env) -> None:
    env.ingest(env.add("Photos/Iceland/glacier.jpg"), env.add("Documents/tax 2025.pdf"))
    with env.engine.connect() as conn:
        tree = TagTree([], {})
        assert [h.title for h in run_search(conn, SearchSpec(text="glacier"), tree)] == [
            "glacier.jpg"
        ]
        assert [h.title for h in run_search(conn, SearchSpec(text="iceland"), tree)] == [
            "glacier.jpg"
        ]
        assert [h.title for h in run_search(conn, SearchSpec(text="tax 2025"), tree)] == [
            "tax 2025.pdf"
        ]
