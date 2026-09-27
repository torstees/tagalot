"""Tests for tag operations (DESIGN.md §7)."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, func, insert, select

from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Entity, EntityTag, Tag, TagAlias
from tagalot.core.tags import (
    MAX_NAME_LENGTH,
    DeleteMode,
    TagError,
    TagService,
    TagTree,
    TagTreeCache,
    add_alias,
    add_tag,
    count_tagged_entities,
    delete_tag,
    merge_tags,
    remove_alias,
    rename_tag,
    reparent_tag,
    set_tag_color,
    subtree_usage,
)
from tagalot.core.writer import DbWriter

# id, parent, name
TAGS = [
    (1, None, "Genre"),
    (2, 1, "Rock"),
    (3, 2, "Punk"),
    (4, 2, "Classic Rock"),
    (5, 1, "Jazz"),
    (6, 5, "Bebop"),
    (7, None, "Mood"),
    (8, 7, "Calm"),
    (9, None, "Music"),
    (10, 9, "Rock"),
    (11, 10, "Punk"),
    (12, 10, "Grunge"),
]
# entity -> tags applied directly
TAGGED = {100: [3], 101: [3, 11], 102: [4], 103: [6, 8], 104: [11]}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(Tag), [{"id": i, "parent_id": p, "name": n} for i, p, n in TAGS])
        conn.execute(
            insert(Entity), [{"id": e, "type": "generic.file", "title": f"E{e}"} for e in TAGGED]
        )
        conn.execute(
            insert(EntityTag),
            [{"entity_id": e, "tag_id": t} for e, ts in TAGGED.items() for t in ts],
        )
        conn.execute(insert(TagAlias).values(tag_id=3, alias="punk rock"))
    yield engine
    engine.dispose()


def _do[R](engine: Engine, op: Callable[[Connection], R]) -> R:
    with engine.begin() as conn:
        return op(conn)


def _tree(engine: Engine) -> TagTree:
    with engine.connect() as conn:
        return TagTree.load(conn)


def _tags_of(engine: Engine, entity_id: int) -> set[int]:
    with engine.connect() as conn:
        return set(conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id == entity_id)))


# --- add ---


def test_add_at_top_and_under_a_parent(engine: Engine) -> None:
    top = _do(engine, lambda c: add_tag(c, None, "  Christmas   Songs "))
    child = _do(engine, lambda c: add_tag(c, 5, "Swing", "#FF8800"))
    tree = _tree(engine)
    assert tree.node(top).name == "Christmas Songs"  # spaces normalized
    assert tree.children(5)[-1] == child  # appended after existing children
    assert tree.node(child).color == "#ff8800"


@pytest.mark.parametrize(
    ("parent", "name", "message"),
    [
        (2, "punk", "already a tag named 'Punk' under 'Rock'"),
        (None, "GENRE", "already a tag named 'Genre' at the top"),
        (2, "   ", "can't be empty"),
        (2, "x" * (MAX_NAME_LENGTH + 1), "at most"),
        (999, "New", "parent tag no longer exists"),
    ],
)
def test_add_refused(engine: Engine, parent: int | None, name: str, message: str) -> None:
    with pytest.raises(TagError, match=message):
        _do(engine, lambda c: add_tag(c, parent, name))


def test_add_bad_color(engine: Engine) -> None:
    with pytest.raises(TagError, match="#rrggbb"):
        _do(engine, lambda c: add_tag(c, None, "X", "red"))


def test_same_name_under_different_parents_is_fine(engine: Engine) -> None:
    _do(engine, lambda c: add_tag(c, 7, "Rock"))


# --- rename ---


def test_rename(engine: Engine) -> None:
    _do(engine, lambda c: rename_tag(c, 3, "Punk Rock"))
    assert _tree(engine).node(3).name == "Punk Rock"


def test_rename_changing_only_case_is_allowed(engine: Engine) -> None:
    _do(engine, lambda c: rename_tag(c, 4, "classic rock"))
    assert _tree(engine).node(4).name == "classic rock"


def test_rename_into_a_sibling_clash(engine: Engine) -> None:
    with pytest.raises(TagError, match="already a tag named 'Classic Rock'"):
        _do(engine, lambda c: rename_tag(c, 3, "CLASSIC ROCK"))


# --- reparent ---


def test_reparent_moves_the_subtree(engine: Engine) -> None:
    _do(engine, lambda c: reparent_tag(c, 5, 7))
    tree = _tree(engine)
    assert tree.node(5).parent_id == 7
    assert tree.ancestors(6) == (7, 5)


def test_reparent_to_top_level(engine: Engine) -> None:
    _do(engine, lambda c: reparent_tag(c, 6, None))
    assert _tree(engine).node(6).parent_id is None


@pytest.mark.parametrize(
    ("tag", "new_parent", "message"),
    [
        (1, 1, "under itself"),
        (1, 3, "under itself or one of its own sub-tags"),
        (2, 9, "already a tag named 'Rock' under 'Music'"),
        (3, 999, "no longer exists"),
    ],
)
def test_reparent_refused(engine: Engine, tag: int, new_parent: int, message: str) -> None:
    with pytest.raises(TagError, match=message):
        _do(engine, lambda c: reparent_tag(c, tag, new_parent))


def test_reparent_to_same_parent_is_a_no_op(engine: Engine) -> None:
    _do(engine, lambda c: reparent_tag(c, 3, 2))
    assert _tree(engine).node(3).parent_id == 2


# --- merge ---


def test_merge_moves_uses_children_and_names(engine: Engine) -> None:
    # Genre/Rock (2: Punk 3, Classic Rock 4) into Music/Rock (10: Punk 11, Grunge 12)
    _do(engine, lambda c: merge_tags(c, 2, 10))
    tree = _tree(engine)
    assert 2 not in tree
    assert 3 not in tree  # Punk clashed with Music/Rock/Punk and merged into it
    assert set(tree.children(10)) == {11, 12, 4}  # Classic Rock moved over
    assert _tags_of(engine, 100) == {11}  # was tagged Genre/Rock/Punk
    assert _tags_of(engine, 101) == {11}  # had both Punks: one row, no duplicate
    assert _tags_of(engine, 102) == {4}
    assert set(tree.aliases(11)) == {"punk rock"}  # Punk's alias came along; name matched
    assert tree.aliases(10) == ()  # "Rock" == "Rock": no alias needed


def test_merge_adds_the_old_name_as_an_alias(engine: Engine) -> None:
    _do(engine, lambda c: merge_tags(c, 6, 5))  # Bebop into Jazz
    tree = _tree(engine)
    assert tree.aliases(5) == ("Bebop",)
    assert _tags_of(engine, 103) == {5, 8}
    assert tree.matching("bebop") == {5}  # the filter box still finds it


@pytest.mark.parametrize(
    ("source", "target", "message"),
    [(3, 3, "into itself"), (1, 3, "into one of its own sub-tags"), (3, 999, "no longer exists")],
)
def test_merge_refused(engine: Engine, source: int, target: int, message: str) -> None:
    with pytest.raises(TagError, match=message):
        _do(engine, lambda c: merge_tags(c, source, target))


def test_merge_into_an_ancestor(engine: Engine) -> None:
    _do(engine, lambda c: merge_tags(c, 3, 1))  # Punk into Genre
    assert _tags_of(engine, 100) == {1}
    assert 3 not in _tree(engine)


# --- delete ---


def test_delete_leaf(engine: Engine) -> None:
    _do(engine, lambda c: delete_tag(c, 4))
    assert 4 not in _tree(engine)
    assert _tags_of(engine, 102) == set()
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(Entity)) == len(TAGGED)


def test_delete_with_children_needs_a_choice(engine: Engine) -> None:
    with pytest.raises(TagError, match="has sub-tags"):
        _do(engine, lambda c: delete_tag(c, 2))


def test_delete_subtree(engine: Engine) -> None:
    _do(engine, lambda c: delete_tag(c, 1, DeleteMode.SUBTREE))
    tree = _tree(engine)
    assert not any(t in tree for t in (1, 2, 3, 4, 5, 6))
    assert _tags_of(engine, 101) == {11}
    assert _tags_of(engine, 103) == {8}


def test_delete_promoting_children(engine: Engine) -> None:
    _do(engine, lambda c: delete_tag(c, 5, DeleteMode.PROMOTE))  # Jazz: Bebop moves to Genre
    tree = _tree(engine)
    assert 5 not in tree
    assert tree.node(6).parent_id == 1
    assert _tags_of(engine, 103) == {6, 8}


def test_promoting_into_a_name_clash_is_refused(engine: Engine) -> None:
    _do(engine, lambda c: add_tag(c, 1, "Punk"))  # Genre/Punk now exists
    with pytest.raises(TagError, match="already has 'Punk'"):
        _do(engine, lambda c: delete_tag(c, 2, DeleteMode.PROMOTE))
    assert 2 in _tree(engine)  # nothing changed


# --- colors and aliases ---


def test_colors(engine: Engine) -> None:
    _do(engine, lambda c: set_tag_color(c, 3, "#AABBCC"))
    assert _tree(engine).node(3).color == "#aabbcc"
    _do(engine, lambda c: set_tag_color(c, 3, None))
    assert _tree(engine).node(3).color is None


def test_aliases(engine: Engine) -> None:
    _do(engine, lambda c: add_alias(c, 8, "chill"))
    _do(engine, lambda c: add_alias(c, 8, "CHILL"))  # duplicate, ignored
    assert _tree(engine).aliases(8) == ("chill",)
    with pytest.raises(TagError, match="must differ"):
        _do(engine, lambda c: add_alias(c, 8, "calm"))
    _do(engine, lambda c: remove_alias(c, 8, "Chill"))
    assert _tree(engine).aliases(8) == ()


# --- counts for confirmations ---


def test_counts(engine: Engine) -> None:
    with engine.connect() as conn:
        assert count_tagged_entities(conn, [3, 11]) == 3  # 100, 101, 104: distinct entities
        assert count_tagged_entities(conn, []) == 0
        assert subtree_usage(conn, 1) == 4  # 100, 101, 102, 103
        assert subtree_usage(conn, 7) == 1


# --- through the writer, with the cache ---


def test_service_writes_through_the_writer_and_refreshes_the_cache(
    engine: Engine, tmp_path: Path
) -> None:
    cache = TagTreeCache(engine)
    with DbWriter(create_keep_engine(tmp_path / "keep.db")) as writer:
        tags = TagService(writer, cache)
        before = cache.get()
        new = tags.add(None, "Christmas")
        assert new in cache.get()
        assert new not in before
        tags.rename(new, "Holiday")
        assert cache.get().node(new).name == "Holiday"
        with pytest.raises(TagError):
            tags.add(None, "holiday")
        tags.merge(6, 5)
        tags.delete(new)
        tags.reparent(5, 7)
        tags.set_color(5, "#123456")
        tags.add_alias(5, "jazz music")
        tags.remove_alias(5, "jazz music")
        tree = cache.get()
        assert new not in tree
        assert tree.node(5).parent_id == 7
        assert tree.aliases(5) == ("Bebop",)
