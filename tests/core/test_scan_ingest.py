"""End to end: scanning a folder with a theme produces searchable entities (issue #49)."""

import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, insert, select

from tagalot.builtin_themes.generic import File, GenericTheme
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, EntityResource, EntityTag, Resource, Tag
from tagalot.core.scanjob import INGEST_BATCH, ScanReport, scan_root
from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tags import TagTree
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import IngestContext, ResourceInfo

T0 = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path

    def scan(self, theme: type = GenericTheme, when: datetime = T0) -> ScanReport:
        root = RootConfig("r", "Files", str(self.files))
        return scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=theme, schema=self.schema
        )

    def search(self, text: str) -> list[str]:
        with self.reader.connect() as conn:
            return [h.title for h in run_search(conn, SearchSpec(text=text), TagTree([], {}))]

    def count(self, model: type) -> int:
        with self.reader.connect() as conn:
            return conn.scalar(select(func.count()).select_from(model)) or 0

    def pending(self) -> list[str]:
        with self.reader.connect() as conn:
            return sorted(
                conn.scalars(select(Resource.relpath).where(Resource.ingested_at.is_(None)))
            )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "Files", ThemeRef("generic", 1)))
    schema = open_theme(engine, keep, GenericTheme).schema
    files = tmp_path / "files"
    for rel in [
        "Photos/Iceland/glacier.jpg",
        "Photos/Iceland/geyser.jpg",
        "Documents/tax 2025.pdf",
    ]:
        (files / rel).parent.mkdir(parents=True, exist_ok=True)
        (files / rel).write_bytes(os.urandom(500))
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, files)
    reader.dispose()


def test_a_scan_makes_entities_you_can_search(env: Env) -> None:
    report = env.scan()
    assert (report.new, report.ingested, report.ingest_errors) == (3, 3, [])
    assert env.count(Entity) == 3
    assert env.count(EntityResource) == 3
    assert env.search("iceland") == ["geyser.jpg", "glacier.jpg"]
    assert env.search("tax 2025") == ["tax 2025.pdf"]
    assert env.pending() == []


def test_rescanning_ingests_only_what_changed(env: Env) -> None:
    env.scan()
    assert env.scan(when=T0 + timedelta(hours=1)).ingested == 0
    (env.files / "Documents/tax 2025.pdf").write_bytes(os.urandom(900))
    (env.files / "Documents/receipt.pdf").write_bytes(b"new")
    report = env.scan(when=T0 + timedelta(hours=2))
    assert (report.changed, report.new, report.ingested) == (1, 1, 2)
    assert env.count(Entity) == 4
    table = env.schema.entities[File].table
    with env.reader.connect() as conn:
        size = conn.scalar(
            select(table.c.size)
            .join(Entity, Entity.id == table.c.id)
            .where(Entity.title == "tax 2025.pdf")
        )
    assert size == 900


def test_a_renamed_folder_keeps_entities_and_tags(env: Env) -> None:
    env.scan()
    with env.reader.connect() as conn:
        glacier = conn.scalar(select(Entity.id).where(Entity.title == "glacier.jpg"))
    env.writer.run(lambda conn: conn.execute(insert(Tag).values(id=1, name="Favorites")))
    env.writer.run(lambda conn: conn.execute(insert(EntityTag).values(entity_id=glacier, tag_id=1)))

    (env.files / "Photos/Iceland").rename(env.files / "Photos/Iceland 2024")
    report = env.scan(when=T0 + timedelta(days=1))
    assert len(report.moves) == 2
    assert env.count(Entity) == 3  # no new entities for moved files
    table = env.schema.entities[File].table
    with env.reader.connect() as conn:
        folder = conn.scalar(select(table.c.folder).where(table.c.id == glacier))
        tagged = conn.scalar(select(EntityTag.entity_id))
    assert folder == "Photos/Iceland 2024"  # the entity was updated for its new place
    assert tagged == glacier
    assert env.search("iceland 2024") == ["geyser.jpg", "glacier.jpg"]


class Flaky(GenericTheme):
    """Fails on any file named like 'bad*', like a theme choking on a corrupt file."""

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            if Path(resource.relpath).name.startswith("bad"):
                raise ValueError(f"can't read {resource.relpath}")
            if resource.relpath.endswith(".pdf"):
                ctx.warn(resource, "no text layer")
        super().ingest(batch, ctx)


def test_a_failing_file_costs_only_itself_and_is_retried_later(env: Env) -> None:
    (env.files / "bad.jpg").write_bytes(b"corrupt")
    report = env.scan(theme=Flaky)
    assert report.ingested == 3
    assert [p for p, _ in report.ingest_errors] == ["bad.jpg"]
    assert "ValueError: can't read bad.jpg" in report.ingest_errors[0][1]
    assert env.pending() == ["bad.jpg"]
    assert env.count(Entity) == 3
    assert [w.message for w in report.ingest_warnings] == ["no text layer"]

    # Next scan (with a fixed theme): the file is retried although it hasn't changed.
    report = env.scan(when=T0 + timedelta(hours=1))
    assert (report.unchanged, report.ingested) == (4, 1)
    assert env.pending() == []
    assert env.search("bad") == ["bad.jpg"]


def test_many_files_across_batches(env: Env) -> None:
    for i in range(INGEST_BATCH * 2 + 17):
        (env.files / f"bulk/{i:04}.txt").parent.mkdir(exist_ok=True)
        (env.files / f"bulk/{i:04}.txt").write_bytes(b"x")
    report = env.scan()
    assert report.ingested == INGEST_BATCH * 2 + 17 + 3
    assert env.count(Entity) == report.ingested


def test_theme_extensions_filter_the_walk(env: Env) -> None:
    class JpegsOnly(GenericTheme):
        extensions = frozenset({".jpg"})

    report = env.scan(theme=JpegsOnly)
    assert (report.new, report.ingested) == (2, 2)
    assert env.search("tax") == []


def test_theme_and_schema_go_together(env: Env) -> None:
    root = RootConfig("r", "Files", str(env.files))
    with pytest.raises(ValueError, match="both theme and schema"):
        scan_root(env.writer, env.reader, root, root.path, theme=GenericTheme)
