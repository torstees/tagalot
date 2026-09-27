"""Tests for the in-memory tag tree and its cache."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, update

from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Tag, TagAlias
from tagalot.core.tags import PATH_SEPARATOR, TagNode, TagTree, TagTreeCache, name_key

# id, parent, name, sort_order
TAGS = [
    (1, None, "Genre", 0),
    (2, 1, "Rock", 0),
    (3, 2, "Punk", 0),
    (4, 2, "Classic Rock", 0),
    (5, 1, "Jazz", 0),
    (6, 5, "Bebop", 0),
    (7, None, "Mood", 1),
    (8, 7, "Calm", 0),
    (9, 7, "Upbeat", 0),
    (10, None, "Christmas", 2),
    (11, 7, "Rock", 0),  # same name as 2, different parent
    (12, None, "Ärger", 3),
]
ALIASES = {3: ["punk rock", "hardcore"], 8: ["chill"]}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(Tag),
            [{"id": i, "parent_id": p, "name": n, "sort_order": o} for i, p, n, o in TAGS],
        )
        conn.execute(
            insert(TagAlias),
            [{"tag_id": t, "alias": a} for t, names in ALIASES.items() for a in names],
        )
    yield engine
    engine.dispose()


@pytest.fixture
def tree(engine: Engine) -> TagTree:
    with engine.connect() as conn:
        return TagTree.load(conn)


def test_loads_every_tag_and_alias(tree: TagTree) -> None:
    assert len(tree) == len(TAGS)
    assert tree.node(3) == TagNode(3, 2, "Punk", None, 0)
    assert set(tree.aliases(3)) == {"punk rock", "hardcore"}
    assert tree.aliases(4) == ()
    assert 99 not in tree
    with pytest.raises(KeyError):
        tree.node(99)


def test_children_in_display_order(tree: TagTree) -> None:
    assert tree.children() == (1, 7, 10, 12)  # top level, by sort_order
    assert tree.children(2) == (4, 3)  # same sort_order: by name, "Classic Rock" < "Punk"
    assert tree.children(3) == ()
    with pytest.raises(KeyError):
        tree.children(99)


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        (1, {1, 2, 3, 4, 5, 6}),  # a parent tag matches its whole subtree (§7)
        (2, {2, 3, 4}),
        (3, {3}),
        (7, {7, 8, 9, 11}),
        (10, {10}),
    ],
)
def test_descendants(tree: TagTree, tag: int, expected: set[int]) -> None:
    assert tree.descendants(tag) == expected
    assert tree.descendants(tag, include_self=False) == expected - {tag}


def test_expand_unions_subtrees(tree: TagTree) -> None:
    assert tree.expand([2, 5]) == {2, 3, 4, 5, 6}
    assert tree.expand([]) == frozenset()
    assert tree.expand([1, 3]) == tree.descendants(1)  # overlapping subtrees


def test_ancestors_and_paths(tree: TagTree) -> None:
    assert tree.ancestors(3) == (1, 2)
    assert tree.ancestors(1) == ()
    assert tree.path(3) == ("Genre", "Rock", "Punk")


def test_display_name_uses_path_only_when_ambiguous(tree: TagTree) -> None:
    assert tree.display_name(3) == "Punk"
    assert tree.display_name(2) == PATH_SEPARATOR.join(["Genre", "Rock"])
    assert tree.display_name(11) == PATH_SEPARATOR.join(["Mood", "Rock"])


@pytest.mark.parametrize(
    ("parent", "name", "expected"),
    [
        (2, "punk", 3),
        (2, "  PUNK ", 3),
        (2, "Bebop", None),  # exists, but under Jazz
        (None, "genre", 1),
        (None, "ärger", 12),  # casefold, beyond ASCII (DESIGN.md §5)
        (None, "ÄRGER", 12),
        (7, "rock", 11),
    ],
)
def test_find_child(tree: TagTree, parent: int | None, name: str, expected: int | None) -> None:
    assert tree.find_child(parent, name) == expected


def test_name_key() -> None:
    assert name_key(" Straße ") == name_key("STRASSE")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("rock", {2, 4, 11, 3}),  # names, plus Punk via its alias "punk rock"
        ("CHILL", {8}),  # alias, case-insensitive
        ("bop", {6}),
        ("zzz", set()),
    ],
)
def test_matching(tree: TagTree, text: str, expected: set[int]) -> None:
    assert tree.matching(text) == expected


def test_matching_empty_text_matches_everything(tree: TagTree) -> None:
    assert tree.matching("  ") == {i for i, *_ in TAGS}


def test_with_ancestors_keeps_matches_visible(tree: TagTree) -> None:
    assert tree.with_ancestors(tree.matching("bebop")) == {1, 5, 6}


def test_corrupt_cycle_does_not_hang() -> None:
    tree = TagTree([TagNode(1, 2, "A", None, 0), TagNode(2, 1, "B", None, 0)], {})
    assert set(tree.ancestors(1)) <= {1, 2}
    assert tree.descendants(1) == {1, 2}  # terminates despite the cycle


# --- the cache ---


def test_cache_reuses_the_tree_until_invalidated(engine: Engine) -> None:
    cache = TagTreeCache(engine)
    first = cache.get()
    assert cache.get() is first
    assert cache.generation == 1

    with engine.begin() as conn:
        conn.execute(update(Tag).where(Tag.id == 3).values(name="Punk Rock"))
    assert cache.get().node(3).name == "Punk"  # stale until invalidated
    cache.invalidate()
    second = cache.get()
    assert second is not first
    assert second.node(3).name == "Punk Rock"
    assert first.node(3).name == "Punk"  # old snapshots never change
    assert cache.generation == 2


def test_cache_is_safe_across_threads(engine: Engine) -> None:
    cache = TagTreeCache(engine)
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            for _ in range(200):
                assert len(cache.get()) == len(TAGS)
        except BaseException as e:
            errors.append(e)

    def invalidator() -> None:
        for _ in range(200):
            cache.invalidate()

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads.append(threading.Thread(target=invalidator))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
