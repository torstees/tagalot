"""The theme's prepare() hook: reading files in the scan worker, outside the write
transaction (#176, DESIGN.md §6, §9)."""

import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from tagalot.core import scanjob
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, Resource
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import IngestContext, ResourceInfo, Theme, field, role

T0 = datetime(2026, 9, 1, tzinfo=UTC)


class Note(ThemeEntity):
    first_line: str | None = field("First line")
    roles = [role("file", kinds={"any"}, primary=True)]


class NotesTheme(Theme):
    """Titles notes by their first line, read in ``prepare``."""

    id, name = "notes", "Notes"
    entities = [Note]
    seen_in_ingest: list[Any] = []

    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        return {r.id: Path(r.path).read_text(encoding="utf-8").splitlines()[0] for r in batch}

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            line = ctx.prepared(resource)
            type(self).seen_in_ingest.append((line, ctx.prepared(resource.id)))
            note = ctx.upsert(Note, resource.relpath, title=line or resource.relpath)
            ctx.update(note, first_line=line)
            ctx.link(note, resource, "file")


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path

    def scan(self, theme: type[Theme], messages: list[str] | None = None) -> ScanReport:
        root = RootConfig("r", "Notes", str(self.files))
        return scan_root(
            self.writer, self.reader, root, root.path, when=T0, theme=theme, schema=self.schema,
            progress=(messages.append if messages is not None else None),
        )  # fmt: skip

    def titles(self) -> list[str]:
        with self.reader.connect() as conn:
            return sorted(conn.scalars(select(Entity.title)))

    def pending(self) -> list[str]:
        with self.reader.connect() as conn:
            return sorted(
                conn.scalars(select(Resource.relpath).where(Resource.ingested_at.is_(None)))
            )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "N", ThemeRef("notes", 1)))
    schema = open_theme(engine, keep, NotesTheme).schema
    files = tmp_path / "files"
    files.mkdir()
    (files / "a.txt").write_text("Shopping\nmilk", encoding="utf-8")
    (files / "b.txt").write_text("Ideas\n…", encoding="utf-8")
    reader = create_keep_engine(keep.db_path, read_only=True)
    NotesTheme.seen_in_ingest = []
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, files)
    reader.dispose()


def test_ingest_gets_what_prepare_read(env: Env) -> None:
    messages: list[str] = []
    report = env.scan(NotesTheme, messages)
    assert (report.ingested, report.ingest_errors) == (2, [])
    assert env.titles() == ["Ideas", "Shopping"]
    assert sorted(NotesTheme.seen_in_ingest) == [("Ideas", "Ideas"), ("Shopping", "Shopping")]
    assert any(m.startswith("Reading 2 of 2") for m in messages)


class Picky(NotesTheme):
    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        if any(r.relpath == "b.txt" for r in batch):
            raise ValueError("unreadable")
        return super().prepare(batch)


def test_a_failing_prepare_costs_only_its_resource(env: Env) -> None:
    report = env.scan(Picky)
    assert report.ingested == 1
    assert report.ingest_errors == [("b.txt", "ValueError: unreadable")]
    assert env.titles() == ["Shopping"]
    assert env.pending() == ["b.txt"]  # tried again by the next scan


class Quiet(NotesTheme):
    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        return {}  # read nothing for anyone


def test_resources_prepare_skipped_get_none(env: Env) -> None:
    env.scan(Quiet)
    assert env.titles() == ["a.txt", "b.txt"]
    assert NotesTheme.seen_in_ingest == [(None, None), (None, None)]


class Plain(NotesTheme):
    prepare = Theme.prepare  # no prepare step at all


def test_themes_without_prepare_are_unchanged(env: Env) -> None:
    messages: list[str] = []
    env.scan(Plain, messages)
    assert env.titles() == ["a.txt", "b.txt"]
    assert not any(m.startswith("Reading") for m in messages)


class Slow(NotesTheme):
    started = threading.Event()
    release = threading.Event()

    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        type(self).started.set()
        assert type(self).release.wait(10)
        return super().prepare(batch)


def test_a_slow_prepare_does_not_block_other_writes(env: Env) -> None:
    Slow.started.clear()
    Slow.release.clear()
    scan = threading.Thread(target=env.scan, args=(Slow,))
    scan.start()
    try:
        assert Slow.started.wait(10)
        # While the theme reads files, the DB writer is free for other work (tagging…).
        assert env.writer.run(lambda conn: "done", timeout=5) == "done"
    finally:
        Slow.release.set()
        scan.join(10)
    assert env.titles() == ["Ideas", "Shopping"]


def test_prepared_outside_a_scan_is_none(env: Env) -> None:
    def work(conn: Any) -> Any:
        return IngestSession(conn, env.schema).prepared(1)

    assert env.writer.run(work) is None


# --- reading on several threads, ahead of ingest (#274) ---


def _many(env: Env, count: int) -> list[str]:
    names = [f"n{i:02}.txt" for i in range(count)]
    for name in names:
        (env.files / name).write_text(f"Title {name}\nbody", encoding="utf-8")
    return names


@pytest.fixture
def small_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scanjob, "INGEST_BATCH", 10)
    monkeypatch.setattr(scanjob, "PREPARE_CHUNK", 3)


class Threaded(NotesTheme):
    threads: set[int] = set()

    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        type(self).threads.add(threading.get_ident())
        time.sleep(0.02)  # a file on a share
        return super().prepare(batch)


def test_reading_is_spread_over_threads(env: Env, small_batches: None) -> None:
    names = _many(env, 45)
    Threaded.threads = set()
    report = env.scan(Threaded)
    assert (report.ingested, report.ingest_errors) == (47, [])
    assert env.titles() == sorted(["Ideas", "Shopping", *(f"Title {n}" for n in names)])
    assert len(Threaded.threads) > 1
    assert threading.get_ident() not in Threaded.threads  # never the scan's own thread
    assert not [t for t in threading.enumerate() if t.name.startswith("prepare")]


class Overlapping(NotesTheme):
    events: list[tuple[str, str]] = []
    lock = threading.Lock()

    def _note(self, what: str, batch: Sequence[ResourceInfo]) -> None:
        with type(self).lock:
            type(self).events.append((what, batch[0].relpath))

    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        self._note("read", batch)
        return super().prepare(batch)

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        self._note("ingest", batch)
        time.sleep(0.2)  # a slow transaction
        super().ingest(batch, ctx)
        self._note("ingested", batch)


def test_the_next_batch_is_read_while_this_one_is_ingested(env: Env, small_batches: None) -> None:
    _many(env, 18)  # with a.txt and b.txt: two batches of 10
    Overlapping.events = []
    env.scan(Overlapping)
    events = Overlapping.events
    first_done = events.index(("ingested", "a.txt"))
    second_batch = [rel for what, rel in events if what == "ingest"][1]
    second_read = events.index(("read", second_batch))
    assert second_read < first_done


class PickyAmongMany(NotesTheme):
    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        if any(r.relpath == "n13.txt" for r in batch):
            raise ValueError("unreadable")
        return super().prepare(batch)


def test_one_unreadable_file_among_many(env: Env, small_batches: None) -> None:
    _many(env, 30)
    report = env.scan(PickyAmongMany)
    assert report.ingested == 31
    assert report.ingest_errors == [("n13.txt", "ValueError: unreadable")]
    assert env.pending() == ["n13.txt"]
