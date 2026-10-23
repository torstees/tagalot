"""Hiding kinds of items (DESIGN.md §8 *Kinds*, #512): file types and item types, over a
small library of books in folders. An item is left out only when every kind it has is
hidden; in a tree, a hidden container stays while it holds a shown item."""

from collections.abc import Iterator, Mapping
from pathlib import PurePosixPath
from typing import Any

import pytest
from sqlalchemy import ColumnElement, Engine, and_, insert, or_

from tagalot.core import closure
from tagalot.core.db import create_keep_engine
from tagalot.core.fts import sync_entities
from tagalot.core.kinds import FILES_KEY, file_kind, is_file_kind
from tagalot.core.models import (
    Base,
    Entity,
    EntityResource,
    Resource,
    ResourceKind,
    Root,
)
from tagalot.core.search import (
    CORE_FIELDS,
    SearchError,
    child_hits,
    count_by_kind,
    count_matches,
    kinds_of,
    run_search,
)
from tagalot.core.search_spec import SearchSpec, SearchSpecError
from tagalot.core.tags import TagTree

FOLDER, BOOK, AUTHOR = "books.folder", "books.book", "books.author"
# id: (type, title, [(relpath, role)])
ENTITIES: dict[int, tuple[str, str, list[tuple[str, str]]]] = {
    1: (FOLDER, "Shelf", [("Shelf", "folder")]),
    2: (FOLDER, "Comics", [("Shelf/Comics", "folder")]),
    10: (BOOK, "Dune", [("Shelf/Dune.epub", "file"), ("Shelf/Dune.pdf", "file"),
                        ("Shelf/dune.jpg", "cover")]),
    11: (BOOK, "Emma", [("Shelf/Emma.pdf", "file")]),
    12: (BOOK, "Scanned", [("Shelf/Scanned.jpeg", "file")]),
    13: (BOOK, "From a note", []),
    20: (BOOK, "Watchmen", [("Shelf/Comics/Watchmen.cbz", "file")]),
    21: (BOOK, "Readme", [("README", "file")]),
    30: (AUTHOR, "Austen", []),
}  # fmt: skip
EDGES = [(1, 10), (1, 11), (1, 12), (1, 13), (1, 2), (2, 20)]
ALL = set(ENTITIES)
FIELDS: Mapping[str, ColumnElement[Any]] = {
    **CORE_FIELDS,
    FILES_KEY: or_(
        and_(Entity.type == BOOK, EntityResource.role == "file"),
        and_(Entity.type == FOLDER, EntityResource.role == "folder"),
    ),
}
BOOK_KINDS = (".epub", ".pdf", ".jpg", ".cbz", ".", BOOK)


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path_factory.mktemp("keep") / "keep.db")
    Base.metadata.create_all(engine)
    files = [(e, path, role) for e, (_, _, links) in ENTITIES.items() for path, role in links]
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
                    "ext": "" if role == "folder" else PurePosixPath(path).suffix,
                }
                for n, (_, path, role) in enumerate(files, start=1)
            ],
        )
        conn.execute(
            insert(EntityResource),
            [
                {"entity_id": e, "resource_id": n, "role": role}
                for n, (e, _, role) in enumerate(files, start=1)
            ],
        )
        closure.add_entities(conn, ENTITIES)
        closure.apply(conn, added=EDGES)
        sync_entities(conn, ENTITIES)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def tree(engine: Engine) -> TagTree:
    with engine.connect() as conn:
        return TagTree.load(conn)


def _ids(engine: Engine, tree: TagTree, spec: SearchSpec) -> set[int]:
    with engine.connect() as conn:
        found = {h.id for h in run_search(conn, spec, tree, limit=None, fields=FIELDS)}
        assert count_matches(conn, spec, tree, FIELDS) == len(found)
    return found


CASES = {
    "nothing hidden": (SearchSpec(), ALL),
    "PDF: a book with an EPUB too stays": (SearchSpec(hidden_kinds=(".pdf",)), ALL - {11}),
    "PDF and EPUB": (SearchSpec(hidden_kinds=(".pdf", ".epub")), ALL - {10, 11}),
    "JPEG is JPG; a cover isn't a file type": (SearchSpec(hidden_kinds=(".jpg",)), ALL - {12}),
    "a .jpeg kind counts for nothing": (SearchSpec(hidden_kinds=(".jpeg",)), ALL),
    "folders": (SearchSpec(hidden_kinds=(FOLDER,)), ALL - {1, 2}),
    "a type hides only its file-less items": (SearchSpec(hidden_kinds=(BOOK,)), ALL - {13}),
    "authors": (SearchSpec(hidden_kinds=(AUTHOR,)), ALL - {30}),
    "no extension": (SearchSpec(hidden_kinds=(".",)), ALL - {21}),
    "an unknown kind": (SearchSpec(hidden_kinds=(".mkv",)), ALL),
    "everything": (SearchSpec(hidden_kinds=(*BOOK_KINDS, FOLDER, AUTHOR)), set()),
    "with other filters": (
        SearchSpec(types=(BOOK,), within=1, hidden_kinds=(".pdf", BOOK)),
        {10, 12, 20},
    ),
    "show contained: the folders' contents": (
        SearchSpec(types=(FOLDER,), show_contained=True, hidden_kinds=(FOLDER,)),
        {10, 11, 12, 13, 20},
    ),
    "show contained: contents hidden too": (
        SearchSpec(types=(FOLDER,), show_contained=True, hidden_kinds=(".pdf", ".cbz")),
        {1, 2, 10, 12, 13},
    ),
    "tree: a hidden folder holding shown books stays": (
        SearchSpec(types=(FOLDER, BOOK), nest=True, hidden_kinds=(FOLDER,)),
        {1, 21},
    ),
    "tree: and goes once they're hidden too": (
        SearchSpec(types=(FOLDER, BOOK), nest=True, hidden_kinds=(FOLDER, *BOOK_KINDS)),
        set(),
    ),
    "tree: a hidden top-level item goes": (
        SearchSpec(types=(FOLDER, BOOK), nest=True, hidden_kinds=(".",)),
        {1},
    ),
}


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_hidden_kinds(engine: Engine, tree: TagTree, case: tuple[SearchSpec, set[int]]) -> None:
    spec, expected = case
    assert _ids(engine, tree, spec) == expected


def test_a_trees_children(engine: Engine, tree: TagTree) -> None:
    spec = SearchSpec(types=(FOLDER, BOOK), nest=True, hidden_kinds=(FOLDER, ".cbz", BOOK))
    with engine.connect() as conn:
        shelf = {h.id for h in child_hits(conn, spec, tree, 1, fields=FIELDS)}
        comics = child_hits(conn, spec, tree, 2, fields=FIELDS)
    assert shelf == {10, 11, 12}  # Comics holds only a hidden comic; 13 has no file
    assert comics == []


def test_counts_by_kind(engine: Engine, tree: TagTree) -> None:
    expected = {
        ".epub": 1, ".pdf": 2, ".jpg": 1, ".cbz": 1, ".": 1,
        BOOK: 1, FOLDER: 2, AUTHOR: 1,
    }  # fmt: skip
    with engine.connect() as conn:
        assert count_by_kind(conn, SearchSpec(), tree, FIELDS) == expected
        hidden = SearchSpec(hidden_kinds=(".pdf", FOLDER))
        assert count_by_kind(conn, hidden, tree, FIELDS) == expected  # counted all the same
        within = SearchSpec(within=1, types=(BOOK,))
        assert count_by_kind(conn, within, tree, FIELDS) == {
            ".epub": 1, ".pdf": 2, ".jpg": 1, ".cbz": 1, BOOK: 1,
        }  # fmt: skip


def test_kinds_of(engine: Engine) -> None:
    with engine.connect() as conn:
        assert kinds_of(conn, [1, 10, 12, 13, 21, 30], FIELDS) == {
            1: frozenset({FOLDER}),
            10: frozenset({".epub", ".pdf"}),
            12: frozenset({".jpg"}),
            13: frozenset({BOOK}),
            21: frozenset({"."}),
            30: frozenset({AUTHOR}),
        }


def test_a_search_without_files_cant_hide_kinds(engine: Engine, tree: TagTree) -> None:
    with engine.connect() as conn, pytest.raises(SearchError):
        run_search(conn, SearchSpec(hidden_kinds=(".pdf",)), tree)
    with engine.connect() as conn:  # nothing hidden: nothing needed
        assert len(run_search(conn, SearchSpec(), tree, limit=None)) == len(ALL)


def test_hidden_kinds_arent_saved() -> None:
    spec = SearchSpec(text="dune", hidden_kinds=(".pdf", FOLDER))
    assert "hidden_kinds" not in spec.to_json()
    assert SearchSpec.from_json(spec.to_json()).hidden_kinds == ()
    with pytest.raises(SearchSpecError):
        SearchSpec(hidden_kinds=("",))


ALIASES = {
    ".jpeg": ".jpg", ".jpe": ".jpg", ".jpg": ".jpg", ".tiff": ".tif", ".htm": ".html",
    ".mpeg": ".mpg", ".yml": ".yaml", ".pdf": ".pdf", "": ".",
}  # fmt: skip


@pytest.mark.parametrize(("ext", "kind"), ALIASES.items())
def test_file_kinds(ext: str, kind: str) -> None:
    assert file_kind(ext) == kind
    assert is_file_kind(kind)


def test_item_types_arent_file_kinds() -> None:
    assert not is_file_kind(FOLDER)
