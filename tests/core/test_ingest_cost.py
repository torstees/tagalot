"""What an ingest costs (#276): statements per new item, and a 50,000-file benchmark.

Ingest ran about 14 statements per new file (19 with the 2D assets theme), each built afresh,
which made it most of a first scan's database time. The session now remembers what it
learned and reuses prebuilt statements. The count test guards that on every run; the
benchmark (``slow``) guards the time.
"""

import re
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event, insert, select

from tagalot.builtin_themes.generic import GenericTheme
from tagalot.core.db import open_keep_database
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.models import Entity, EntityResource, Resource, ResourceKind, Root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import ResourceInfo

BATCH = 100


@pytest.fixture
def keep(tmp_path: Path) -> Iterator[tuple[Engine, ThemeSchema]]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "F", ThemeRef("generic", 1)))
    schema = open_theme(engine, keep, GenericTheme).schema
    with engine.begin() as conn:
        conn.execute(insert(Root).values(id="r", name="Files", online=True))
    yield engine, schema
    engine.dispose()


def _files(engine: Engine, count: int) -> list[ResourceInfo]:
    rows = [
        {
            "root_id": "r",
            "relpath": f"Folder {i // 100}/file {i}.txt",
            "kind": ResourceKind.FILE,
            "ext": ".txt",
            "size": 10 + i,
            "mtime_ns": 1_000 + i,
        }
        for i in range(count)
    ]
    with engine.begin() as conn:
        conn.execute(insert(Resource), rows)
        found = conn.execute(select(Resource.id, Resource.relpath).order_by(Resource.id)).all()
    return [
        ResourceInfo(rid, "r", rel, "file", ".txt", 10 + n, 1_000 + n, f"/files/{rel}")
        for n, (rid, rel) in enumerate(found)
    ]


def _ingest(engine: Engine, schema: ThemeSchema, files: list[ResourceInfo]) -> None:
    theme = GenericTheme()
    for start in range(0, len(files), BATCH):
        with engine.begin() as conn:
            ctx = IngestSession(conn, schema)
            theme.ingest(files[start : start + BATCH], ctx)
            ctx.flush()


def test_statements_per_new_file(keep: tuple[Engine, ThemeSchema]) -> None:
    engine, schema = keep
    files = _files(engine, 300)
    statements: Counter[str] = Counter()

    def count(*args: Any) -> None:
        statements[re.sub(r"\s+", " ", args[2])[:60]] += 1

    event.listen(engine, "before_cursor_execute", count)
    try:
        _ingest(engine, schema, files)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    with engine.connect() as conn:
        assert conn.scalar(select(Entity.id).order_by(Entity.id.desc())) is not None
        assert len(conn.execute(select(EntityResource.entity_id)).all()) == 300
    per_file = sum(statements.values()) / len(files)
    # Was 14.1. Now: whose file is it, the key, the item, its row, its provenance, its link.
    assert per_file <= 7, statements.most_common(12)


@pytest.mark.slow
def test_ingesting_50000_files(keep: tuple[Engine, ThemeSchema]) -> None:
    engine, schema = keep
    files = _files(engine, 50_000)
    start = time.perf_counter()
    _ingest(engine, schema, files)
    seconds = time.perf_counter() - start
    with engine.connect() as conn:
        assert len(conn.execute(select(Entity.id)).all()) == 50_000
    # 13 s on the development machine (122 s before #276); generous for CI runners.
    assert seconds < 60, f"{seconds:.1f} s"
