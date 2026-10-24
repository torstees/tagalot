"""Files inside archives as resources (#391, DESIGN.md §6 *Archive members*): read whole
by a root's ``archives`` patterns or the user's choice, or member by member; following
their archive offline, missing, skipped, and moved; never written to."""

import os
import time
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select

from tagalot.builtin_themes.generic import File, GenericTheme
from tagalot.core.archive_members import (
    member_choices,
    set_archive_mode,
    set_member_choices,
)
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import (
    ArchiveMode,
    ArchiveRead,
    Entity,
    EntityResource,
    EntityTag,
    Resource,
    ResourceStatus,
    Tag,
)
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tags import TagTree
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writeback import _markdown_files
from tagalot.core.writer import DbWriter
from tagalot.themes.api import IngestContext, ResourceInfo

T0 = datetime(2026, 10, 1, tzinfo=UTC)
PACK = "Fonts/Aurora/pack.zip"
MEMBERS = {
    "fonts/Regular.ttf": b"regular font",
    "fonts/Bold.ttf": b"bold font",
    "readme.md": b"# Pack\n",
}


def _zip(path: Path, members: Mapping[str, bytes], *, extra: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("fonts/", b"")  # a folder entry
        for name, data in members.items():
            zf.writestr(zipfile.ZipInfo(name, (2024, 5, 1, 12, 0, 0)), data)
        if extra:  # never resources
            zf.writestr("__MACOSX/fonts/._Regular.ttf", b"x")
            zf.writestr(".hidden", b"x")
            zf.writestr("inner.zip", b"PK\x05\x06" + b"\x00" * 18)


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path
    archives: list[str] = field(default_factory=list)

    def scan(self, theme: type = GenericTheme, when: datetime = T0) -> ScanReport:
        root = RootConfig("r", "Files", str(self.files), archives=self.archives)
        return scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=theme, schema=self.schema
        )

    def members(self) -> dict[str, tuple[ResourceStatus, bool]]:
        """Members' relpaths: (status, skipped)."""
        with self.reader.connect() as conn:
            rows = conn.execute(
                select(Resource.relpath, Resource.status, Resource.skipped).where(
                    Resource.parent_resource_id.is_not(None)
                )
            )
            return {rel: (status, skipped) for rel, status, skipped in rows}

    def shown(self) -> set[str]:
        return {
            rel
            for rel, (status, skipped) in self.members().items()
            if status is ResourceStatus.OK and not skipped
        }

    def resource(self, relpath: str) -> int:
        with self.reader.connect() as conn:
            found = conn.scalar(select(Resource.id).where(Resource.relpath == relpath))
        assert found is not None, relpath
        return found

    def entity_of(self, relpath: str) -> int:
        with self.reader.connect() as conn:
            found = conn.scalar(
                select(EntityResource.entity_id).where(
                    EntityResource.resource_id == self.resource(relpath)
                )
            )
        assert found is not None, relpath
        return found

    def run(self, job: Any) -> Any:
        return self.writer.run(job)

    def listed(self) -> set[int]:
        """The items a search of everything lists."""
        with self.reader.connect() as conn:
            hits = run_search(conn, SearchSpec(), TagTree.load(conn), limit=None)
            return {h.id for h in hits}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "Files", ThemeRef("generic", 1)))
    schema = open_theme(engine, keep, GenericTheme).schema
    files = tmp_path / "files"
    _zip(files / PACK, MEMBERS)
    _zip(files / "Other/other.zip", {"a.txt": b"a"})
    (files / "Fonts/notes.txt").write_bytes(os.urandom(50))
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, files)
    reader.dispose()


ALL = {f"{PACK}/{name}" for name in MEMBERS}


def test_nothing_is_read_inside_archives_unless_chosen(env: Env) -> None:
    report = env.scan()
    assert env.members() == {}
    assert report.members.new == 0


def test_a_pattern_reads_its_archives_whole(env: Env) -> None:
    env.archives = ["Fonts/**"]
    report = env.scan()
    assert env.shown() == ALL  # not folders, __MACOSX, hidden files, or the inner zip
    assert report.members.new == 3
    assert report.ingest_errors == []
    with env.reader.connect() as conn:
        regular = conn.execute(
            select(
                Resource.parent_resource_id,
                Resource.ext,
                Resource.size,
                Resource.fingerprint,
                Resource.ingested_at,
            ).where(Resource.relpath == f"{PACK}/fonts/Regular.ttf")
        ).one()
        titles = set(conn.scalars(select(Entity.title)))
    assert regular.parent_resource_id == env.resource(PACK)
    assert (regular.ext, regular.size) == (".ttf", len(MEMBERS["fonts/Regular.ttf"]))
    assert regular.fingerprint is not None  # from its extracted copy
    assert regular.ingested_at is not None
    assert {"Regular.ttf", "Bold.ttf", "readme.md", "pack.zip"} <= titles
    assert "a.txt" not in titles  # Other/ isn't under the pattern

    again = env.scan(when=T0 + timedelta(hours=1))  # unchanged: not listed or read again
    assert (again.members.new, again.ingested) == (0, 0)


class Fonts(GenericTheme):
    """The generic theme taking only fonts and archives: other members aren't recorded."""

    extensions = frozenset({".ttf", ".zip"})


def test_members_the_theme_doesnt_take_are_left_out(env: Env) -> None:
    env.archives = ["**"]
    env.scan(theme=Fonts)
    assert env.shown() == {f"{PACK}/fonts/Regular.ttf", f"{PACK}/fonts/Bold.ttf"}


class Seeing(GenericTheme):
    """Records what the theme is told about members, and that it can read them."""

    seen: dict[str, tuple[str, str | None, str, bytes]] = {}

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for r in batch:
            if r.archive is not None:
                Seeing.seen[r.relpath] = (
                    r.folder,
                    r.archive,
                    r.folder_path,
                    Path(r.path).read_bytes(),
                )
        super().ingest(batch, ctx)


def test_the_theme_reads_a_copy_counting_in_the_archives_folder(env: Env) -> None:
    env.archives = ["**"]
    env.scan(theme=Seeing)
    folder, archive, folder_path, data = Seeing.seen[f"{PACK}/fonts/Regular.ttf"]
    assert (folder, archive) == ("Fonts/Aurora", PACK)
    assert Path(folder_path) == env.files / "Fonts" / "Aurora"
    assert data == MEMBERS["fonts/Regular.ttf"]


def test_choosing_member_by_member(env: Env) -> None:
    env.scan()
    pack = env.resource(PACK)
    env.run(
        lambda conn: set_member_choices(conn, pack, {"fonts/Regular.ttf": True, "readme.md": False})
    )
    report = env.scan(when=T0 + timedelta(hours=1))
    assert env.shown() == {f"{PACK}/fonts/Regular.ttf"}
    assert report.members.undecided == 1  # Bold: listed for review, not read
    with env.reader.connect() as conn:
        assert member_choices(conn, pack) == {
            "fonts/Regular.ttf": True,
            "readme.md": False,
            "fonts/Bold.ttf": None,
        }

    # Tag it, hide it, show it again: its item and tag come back with it.
    regular = env.entity_of(f"{PACK}/fonts/Regular.ttf")
    env.run(lambda conn: conn.execute(insert(Tag).values(id=1, name="Serif")))
    env.run(lambda conn: conn.execute(insert(EntityTag).values(entity_id=regular, tag_id=1)))
    env.run(
        lambda conn: set_member_choices(
            conn, pack, {"fonts/Regular.ttf": False, "fonts/Bold.ttf": True}
        )
    )
    env.scan(when=T0 + timedelta(hours=2))
    assert env.shown() == {f"{PACK}/fonts/Bold.ttf"}
    assert env.members()[f"{PACK}/fonts/Regular.ttf"] == (ResourceStatus.OK, True)  # skipped
    env.run(lambda conn: set_member_choices(conn, pack, {"fonts/Regular.ttf": True}))
    env.scan(when=T0 + timedelta(hours=3))
    assert f"{PACK}/fonts/Regular.ttf" in env.shown()
    assert env.entity_of(f"{PACK}/fonts/Regular.ttf") == regular
    with env.reader.connect() as conn:
        assert conn.scalar(select(EntityTag.tag_id).where(EntityTag.entity_id == regular)) == 1


@pytest.mark.parametrize(
    ("mode", "italic_read"), [(ArchiveMode.ALL, True), (ArchiveMode.CHOSEN, False)]
)
def test_a_newer_archive_with_more_members(env: Env, mode: ArchiveMode, italic_read: bool) -> None:
    """Whole: new members are read. Member by member: they wait, undecided (#520)."""
    env.scan()
    pack = env.resource(PACK)
    if mode is ArchiveMode.CHOSEN:
        env.run(lambda conn: set_member_choices(conn, pack, dict.fromkeys(MEMBERS, True)))
    else:
        env.run(lambda conn: set_archive_mode(conn, pack, ArchiveMode.ALL))
    env.scan(when=T0 + timedelta(hours=1))
    assert env.shown() == ALL
    newer = dict(MEMBERS) | {"fonts/Italic.ttf": b"italic"}
    del newer["readme.md"]
    _zip(env.files / PACK, newer)
    os.utime(env.files / PACK, (T0.timestamp() + 7200, T0.timestamp() + 7200))
    report = env.scan(when=T0 + timedelta(hours=2))
    assert (f"{PACK}/fonts/Italic.ttf" in env.shown()) is italic_read
    assert env.members()[f"{PACK}/readme.md"][0] is ResourceStatus.MISSING
    assert report.members.missing == 1
    assert report.members.undecided == (0 if italic_read else 1)


def test_members_follow_their_archive(env: Env) -> None:
    env.archives = ["Fonts/**"]
    env.scan()

    # Its root unreachable, then back.
    root = RootConfig("r", "Files", str(env.files / "gone"), archives=env.archives)
    scan_root(env.writer, env.reader, root, root.path, theme=GenericTheme, schema=env.schema)
    assert {s for s, _ in env.members().values()} == {ResourceStatus.OFFLINE}
    env.scan(when=T0 + timedelta(hours=1))
    assert env.shown() == ALL

    # Chosen none, over the pattern: skipped, kept, and their items hidden (#526).
    pack = env.resource(PACK)
    items = {env.entity_of(rel) for rel in ALL}
    assert items <= env.listed()
    env.run(lambda conn: set_archive_mode(conn, pack, ArchiveMode.NONE))
    env.scan(when=T0 + timedelta(hours=2))
    assert env.members() == dict.fromkeys(ALL, (ResourceStatus.OK, True))
    assert not items & env.listed()
    env.run(lambda conn: set_archive_mode(conn, pack, None))  # the pattern again
    env.scan(when=T0 + timedelta(hours=3))
    assert env.shown() == ALL
    assert items <= env.listed()

    # The archive deleted: its members are missing too.
    (env.files / PACK).unlink()
    env.scan(when=T0 + timedelta(hours=4))
    assert {s for s, _ in env.members().values()} == {ResourceStatus.MISSING}


def test_members_move_with_their_archive(env: Env) -> None:
    env.archives = ["Fonts/**"]
    env.scan()
    regular = env.resource(f"{PACK}/fonts/Regular.ttf")
    pack = env.resource(PACK)
    env.run(lambda conn: set_archive_mode(conn, pack, ArchiveMode.ALL))
    moved = "Fonts/Kept/pack.zip"
    (env.files / moved).parent.mkdir(parents=True)
    (env.files / PACK).rename(env.files / moved)
    report = env.scan(when=T0 + timedelta(hours=1))
    assert len(report.moves) == 1
    assert env.resource(f"{moved}/fonts/Regular.ttf") == regular  # same resource, new path
    assert env.shown() == {f"{moved}/{name}" for name in MEMBERS}
    assert report.members.new == 0
    with env.reader.connect() as conn:
        mode = conn.scalar(
            select(ArchiveRead.mode).where(ArchiveRead.resource_id == env.resource(moved))
        )
    assert mode is ArchiveMode.ALL  # the choice moved too


def test_write_to_file_never_picks_a_member(env: Env) -> None:
    """It's marked, for Write to file… to say why it isn't written (#524)."""
    env.archives = ["**"]
    env.scan()
    readme = env.entity_of(f"{PACK}/readme.md")
    with env.reader.connect() as conn:
        [found] = _markdown_files(conn, env.schema, readme, File)
    assert found[2] == f"{PACK}/readme.md"
    assert found[5] is True  # inside an archive


@pytest.mark.slow
def test_a_large_archive(env: Env) -> None:
    """5,000 members: read once in a generous time, and not listed again unchanged."""
    _zip(env.files / "Big/big.zip", {f"pages/{n:05}.txt": b"x" * 200 for n in range(5000)})
    env.archives = ["Big/**"]
    start = time.perf_counter()
    report = env.scan()
    first = time.perf_counter() - start
    assert report.members.new == 5000
    assert report.ingest_errors == []
    start = time.perf_counter()
    again = env.scan(when=T0 + timedelta(hours=1))
    second = time.perf_counter() - start
    print(f"first scan {first:.1f} s, unchanged {second:.2f} s")
    assert again.ingested == 0
    assert first < 60
    assert second < 5
