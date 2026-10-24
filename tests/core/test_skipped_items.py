"""Items whose files are all skipped are hidden (#526, DESIGN.md §6 *Scanning*): from
searches and lists, contents, and the dashboard; an item with a file still read, or with no
files, stays; unskipping its files brings it back with its tags."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, select, update

from tagalot.builtin_themes.generic import GenericTheme
from tagalot.core import closure
from tagalot.core.dashboard import load_dashboard
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import (
    Base,
    Entity,
    EntityResource,
    EntityTag,
    Resource,
    ResourceKind,
    Root,
    Tag,
)
from tagalot.core.scanjob import scan_root
from tagalot.core.search import child_hits, count_by_type, count_matches, run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.skipped_items import skipped_items
from tagalot.core.tags import TagTree
from tagalot.core.theme_db import open_theme
from tagalot.core.writer import DbWriter

FOLDER, BOOK = "books.folder", "books.book"
# id: (type, title, [(relpath, role, skipped)])
ENTITIES: dict[int, tuple[str, str, list[tuple[str, str, bool]]]] = {
    1: (FOLDER, "Shelf", [("Shelf", "folder", True)]),
    10: (BOOK, "Dune", [("Shelf/Dune.epub", "file", False), ("Shelf/Dune.pdf", "file", True)]),
    11: (BOOK, "Emma", [("Shelf/Emma.pdf", "file", True), ("Shelf/Emma.jpg", "cover", True)]),
    12: (BOOK, "Scanned", [("Shelf/Scanned.pdf", "file", True), ("Shelf/s.jpg", "cover", False)]),
    13: (BOOK, "From a note", []),
    20: (FOLDER, "Comics", [("Comics", "folder", False)]),
    21: (BOOK, "Watchmen", [("Comics/Watchmen.cbz", "file", True)]),
    22: (BOOK, "Maus", [("Comics/Maus.cbz", "file", False)]),
}  # fmt: skip
EDGES = [(1, 10), (1, 11), (1, 12), (1, 13), (20, 21), (20, 22)]
HIDDEN = {1, 11, 21}  # every file skipped
ALL = set(ENTITIES)


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path_factory.mktemp("keep") / "keep.db")
    Base.metadata.create_all(engine)
    files = [(e, *link) for e, (_, _, links) in ENTITIES.items() for link in links]
    with engine.begin() as conn:
        conn.execute(insert(Root).values(id="r", name="Books"))
        conn.execute(
            insert(Entity),
            [{"id": i, "type": t, "title": ti} for i, (t, ti, _) in ENTITIES.items()],
        )
        conn.execute(
            insert(Resource),
            [
                {
                    "id": n,
                    "root_id": "r",
                    "relpath": path,
                    "kind": ResourceKind.DIR if role == "folder" else ResourceKind.FILE,
                    "skipped": skipped,
                }
                for n, (_, path, role, skipped) in enumerate(files, start=1)
            ],
        )
        conn.execute(
            insert(EntityResource),
            [
                {"entity_id": e, "resource_id": n, "role": role}
                for n, (e, _, role, _) in enumerate(files, start=1)
            ],
        )
        closure.apply(conn, added=EDGES)
    yield engine
    engine.dispose()


def _ids(engine: Engine, spec: SearchSpec) -> set[int]:
    with engine.connect() as conn:
        return {h.id for h in run_search(conn, spec, TagTree.load(conn), limit=None)}


def test_items_whose_files_are_all_skipped(engine: Engine) -> None:
    with engine.connect() as conn:
        assert set(conn.scalars(skipped_items())) == HIDDEN


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (SearchSpec(), ALL - HIDDEN),
        (SearchSpec(types=(BOOK,)), {10, 12, 13, 22}),
        (SearchSpec(within=1), {10, 12, 13}),
        (SearchSpec(within=20), {22}),
        # Contained items are hidden too.
        (SearchSpec(types=(FOLDER,), show_contained=True), {20, 22}),
        # A tree: Shelf is hidden, so what it holds is listed at the top.
        (SearchSpec(nest=True), {10, 12, 13, 20}),
    ],
    ids=["all", "type", "within hidden", "within", "show contained", "tree"],
)
def test_searches_leave_them_out(engine: Engine, spec: SearchSpec, expected: set[int]) -> None:
    assert _ids(engine, spec) == expected
    with engine.connect() as conn:
        assert count_matches(conn, spec, TagTree.load(conn)) == len(expected)


def test_counts_and_tree_children_leave_them_out(engine: Engine) -> None:
    with engine.connect() as conn:
        tree = TagTree.load(conn)
        assert count_by_type(conn, SearchSpec(), tree) == {FOLDER: 1, BOOK: 4}
        assert {h.id for h in child_hits(conn, SearchSpec(nest=True), tree, 20)} == {22}


# --- end to end: a Skip pattern, with the generic theme ---

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def test_a_skip_pattern_hides_items_until_it_is_removed(tmp_path: Path) -> None:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "Files", ThemeRef("generic", 1)))
    schema = open_theme(engine, keep, GenericTheme).schema
    files = tmp_path / "files"
    (files / "Drafts").mkdir(parents=True)
    (files / "kept.txt").write_text("kept")
    (files / "Drafts/draft.txt").write_text("draft")
    reader = create_keep_engine(keep.db_path, read_only=True)

    def scan(exclude: list[str], when: datetime) -> None:
        root = RootConfig("r", "Files", str(files), exclude=exclude)
        scan_root(writer, reader, root, root.path, when=when, theme=GenericTheme, schema=schema)

    def titles() -> set[str]:
        with reader.connect() as conn:
            tree = TagTree.load(conn)
            return {h.title for h in run_search(conn, SearchSpec(), tree, limit=None)}

    def dashboard_total() -> int:
        with reader.connect() as conn:
            return load_dashboard(conn, schema, TagTree.load(conn)).total

    with DbWriter(engine) as writer:
        scan([], T0)
        assert {"kept.txt", "draft.txt"} <= titles()
        shown = dashboard_total()
        with reader.connect() as conn:
            draft = conn.scalar(select(Entity.id).where(Entity.title == "draft.txt"))
        writer.run(lambda conn: conn.execute(insert(Tag).values(id=1, name="Rough")))
        writer.run(lambda conn: conn.execute(insert(EntityTag).values(entity_id=draft, tag_id=1)))

        scan(["Drafts/**"], T0 + timedelta(hours=1))
        assert "draft.txt" not in titles()
        assert "kept.txt" in titles()
        assert dashboard_total() == shown - 1

        scan([], T0 + timedelta(hours=2))
        assert "draft.txt" in titles()
        with reader.connect() as conn:
            assert conn.scalar(select(EntityTag.tag_id).where(EntityTag.entity_id == draft)) == 1
    reader.dispose()
    engine.dispose()


def test_a_file_read_again_shows_its_item(engine: Engine) -> None:
    """Hiding is worked out from the files each time, nothing is stored: unskip one file
    and its item is back."""
    with engine.begin() as conn:
        conn.execute(
            update(Resource).where(Resource.relpath == "Shelf/Emma.pdf").values(skipped=False)
        )
    try:
        assert 11 in _ids(engine, SearchSpec())
    finally:
        with engine.begin() as conn:
            conn.execute(
                update(Resource).where(Resource.relpath == "Shelf/Emma.pdf").values(skipped=True)
            )
