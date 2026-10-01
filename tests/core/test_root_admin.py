"""Managing roots: keep.toml edits, stop watching, and removing a root with its items (#109)."""

import importlib.util
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select

from tagalot.core import closure
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import (
    DEFAULT_EXCLUDES,
    KeepConfig,
    KeepError,
    RootConfig,
    ThemeRef,
    create_keep,
    load_keep_config,
    root_id_for,
    save_keep_config,
)
from tagalot.core.models import Entity, EntityTag, Resource, ResourceStatus, Root, entity_fts
from tagalot.core.root_admin import (
    add_root,
    delete_root,
    edit_root,
    removal_counts,
    root_statuses,
)
from tagalot.core.scanjob import scan_root
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import add_tag
from tagalot.core.theme_db import open_theme
from tagalot.core.writer import DbWriter
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import IngestContext, ResourceInfo, Theme, contains, role

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


# --- keep.toml edits ---


def _config(tmp_path: Path) -> KeepConfig:
    keep = create_keep(
        tmp_path / "k",
        "K",
        ThemeRef("generic", 1),
        [RootConfig("photos", "Photos", str(tmp_path / "Photos"))],
    )
    return keep.config


def test_adding_a_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    keep_dir = tmp_path / "k"
    new = add_root(config, keep_dir, "", str(tmp_path / "Other" / "photos"))
    added = new.roots[-1]
    assert (added.id, added.name) == ("photos-2", str(tmp_path / "Other" / "photos"))
    assert added.exclude == list(DEFAULT_EXCLUDES)
    assert len(config.roots) == 1  # the original is untouched
    with pytest.raises(KeepError, match="already watches"):
        add_root(config, keep_dir, "Again", str(tmp_path / "Photos"))
    with pytest.raises(KeepError, match="inside one another"):
        add_root(config, keep_dir, "Inner", str(tmp_path / "Photos" / "2024"))
    with pytest.raises(KeepError, match="keep folder"):
        add_root(config, keep_dir, "Keep", str(keep_dir / "sub"))
    with pytest.raises(KeepError, match="Choose a folder"):
        add_root(config, keep_dir, "Blank", "  ")


def test_editing_a_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    keep_dir = tmp_path / "k"
    new = edit_root(
        config,
        keep_dir,
        "photos",
        name=" Pictures ",
        path=str(tmp_path / "Pictures"),
        exclude=["**/*.tmp", "  ", " **/cache/** "],
    )
    root = new.roots[0]
    assert (root.id, root.name, root.path) == ("photos", "Pictures", str(tmp_path / "Pictures"))
    assert root.exclude == ["**/*.tmp", "**/cache/**"]
    with pytest.raises(KeepError, match="needs a name"):
        edit_root(config, keep_dir, "photos", name=" ")
    with pytest.raises(KeepError, match="no root"):
        edit_root(config, keep_dir, "nope", name="x")


def test_an_unwatched_root_is_saved_and_re_adding_its_folder_watches_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    keep_dir = tmp_path / "k"
    paused = edit_root(config, keep_dir, "photos", watched=False)
    save_keep_config(paused, keep_dir / "keep.toml")
    assert "watched = false" in (keep_dir / "keep.toml").read_text(encoding="utf-8")
    assert load_keep_config(keep_dir / "keep.toml").roots[0].watched is False
    again = add_root(paused, keep_dir, "Photos again", str(tmp_path / "Photos"))
    assert [(r.id, r.watched) for r in again.roots] == [("photos", True)]  # reconnected


@pytest.mark.parametrize(
    ("folder", "taken", "root_id"),
    [
        ("D:/My Photos", (), "my-photos"),
        ("\\\\nas\\music", ("music",), "music-2"),
        ("/", (), "root"),
    ],
)
def test_root_ids(folder: str, taken: tuple[str, ...], root_id: str) -> None:
    assert root_id_for(folder, taken) == root_id


# --- removing a root with its items ---


class Box(ThemeEntity):
    """A top-level folder; it links no file."""


class Thing(ThemeEntity):
    roles = [role("file", kinds={"any"}, many=True, primary=True)]


class Boxes(Theme):
    """Each .txt file is a Thing keyed by its name, so the same name in two roots is one
    thing with two files; things sit in a Box per top folder."""

    id, name, version = "boxes", "Boxes", 1
    extensions = frozenset({".txt"})
    entities = [Box, Thing]
    containment = [contains(Box, Thing)]

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            folder, _, name = resource.relpath.rpartition("/")
            thing = ctx.upsert(Thing, name, title=name)
            ctx.link(thing, resource, "file")
            if folder:
                ctx.contain(ctx.upsert(Box, folder, title=folder), thing)


@pytest.fixture
def two_roots(tmp_path: Path) -> Iterator[tuple[DbWriter, object]]:
    a, b = tmp_path / "A", tmp_path / "B"
    for path in ("A/boxA/one.txt", "A/boxA/two.txt", "A/shared/three.txt"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(path, encoding="utf-8")
    for path in ("B/shared/three.txt", "B/boxB/four.txt"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(path, encoding="utf-8")
    roots = [RootConfig("a", "A", str(a)), RootConfig("b", "B", str(b))]
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("boxes", 1), roots))
    schema = open_theme(engine, keep, Boxes).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        for root in roots:
            scan_root(writer, reader, root, root.path, theme=Boxes, schema=schema)
        yield writer, schema
    reader.dispose()


def _titles(writer: DbWriter) -> list[str]:
    return sorted(writer.run(lambda conn: list(conn.scalars(select(Entity.title)))))


def test_removing_a_root_deletes_items_that_had_files_only_there(
    two_roots: tuple[DbWriter, object],
) -> None:
    writer, schema = two_roots
    assert _titles(writer) == [
        "boxA", "boxB", "four.txt", "one.txt", "shared", "three.txt", "two.txt",
    ]  # fmt: skip
    tag = writer.run(lambda conn: add_tag(conn, None, "Tagged"))
    writer.run(
        lambda conn: conn.execute(
            insert(EntityTag).values(
                entity_id=conn.scalar(select(Entity.id).where(Entity.title == "one.txt")),
                tag_id=tag,
            )
        )
    )
    counts = writer.run(lambda conn: removal_counts(conn, "a"))
    assert (counts.deleted, counts.kept) == (3, 1)  # one, two, and boxA; three is in B too
    assert writer.run(lambda conn: delete_root(conn, schema, "a")) == counts  # type: ignore[arg-type]

    assert _titles(writer) == ["boxB", "four.txt", "shared", "three.txt"]
    with_tags = writer.run(lambda conn: conn.scalar(select(func.count()).select_from(EntityTag)))
    assert with_tags == 0
    roots = writer.run(lambda conn: list(conn.scalars(select(Root.id))))
    assert roots == ["b"]
    left = writer.run(lambda conn: list(conn.scalars(select(Resource.relpath))))
    assert sorted(left) == ["boxB/four.txt", "shared/three.txt"]
    assert writer.run(lambda conn: closure.verify(conn).ok)
    search_rows = writer.run(lambda conn: conn.scalar(select(func.count()).select_from(entity_fts)))
    assert search_rows == 4


# --- the session ---


@pytest.fixture
def music(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_music_demo(tmp_path), Settings()) as session:
        yield session


def _statuses(session: KeepSession) -> set[ResourceStatus]:
    with session.reader.connect() as conn:
        return set(conn.scalars(select(Resource.status)))


def test_stop_watching_keeps_everything_and_watching_again_reconnects(
    music: KeepSession,
) -> None:
    with music.reader.connect() as conn:
        items = conn.scalar(select(func.count()).select_from(Entity))
    music.stop_watching("music")
    assert music.keep.config.roots[0].watched is False
    assert load_keep_config(music.keep.toml_path).roots[0].watched is False
    assert _statuses(music) == {ResourceStatus.OFFLINE}
    assert music.scan_all() == []  # not scanned
    with music.reader.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(Entity)) == items
        status = root_statuses(conn)["music"]
    assert (status.online, status.last_error, status.offline) == (
        False,
        "Not watched",
        24,
    )  # files and folders

    music.watch_again("music")
    [report] = music.scan_all()
    assert report.online
    assert _statuses(music) == {ResourceStatus.OK}
    with music.reader.connect() as conn:
        status = root_statuses(conn)["music"]
    assert (status.online, status.last_error, status.files, status.offline) == (True, None, 24, 0)


def test_removing_the_root_with_its_items(music: KeepSession) -> None:
    music.settings.set_root_override(music.keep.config.id, "music", "Z:/elsewhere")
    counts = music.delete_root("music")
    assert counts.deleted == 24  # every artist, album, and song
    assert load_keep_config(music.keep.toml_path).roots == []
    assert music.settings.root_overrides == {}
    with music.reader.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(Entity)) == 0
        assert conn.scalar(select(func.count()).select_from(Resource)) == 0
