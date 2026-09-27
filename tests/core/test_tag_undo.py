"""Tests for the session undo/redo history of tag operations."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, delete, insert, select

from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Entity, EntityTag, Tag, TagAlias
from tagalot.core.tag_service import MAX_HISTORY, TagService
from tagalot.core.tags import DeleteMode, TagError, TagTreeCache
from tagalot.core.writer import DbWriter

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
]
TAGGED = {100: [3], 101: [3, 11], 102: [4], 103: [6, 8], 104: [11]}

State = tuple[frozenset[tuple[object, ...]], ...]


@dataclass
class Env:
    engine: Engine
    tags: TagService

    def state(self) -> State:
        """Everything a tag operation can change."""
        queries = [
            select(Tag.id, Tag.parent_id, Tag.name, Tag.color, Tag.sort_order),
            select(TagAlias.tag_id, TagAlias.alias),
            select(EntityTag.entity_id, EntityTag.tag_id, EntityTag.added_at),
        ]
        with self.engine.connect() as conn:
            return tuple(frozenset(tuple(row) for row in conn.execute(q)) for q in queries)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(Tag), [{"id": i, "parent_id": p, "name": n} for i, p, n in TAGS])
        conn.execute(insert(Entity), [{"id": e, "type": "t", "title": f"E{e}"} for e in TAGGED])
        conn.execute(
            insert(EntityTag),
            [{"entity_id": e, "tag_id": t} for e, ts in TAGGED.items() for t in ts],
        )
        conn.execute(insert(TagAlias).values(tag_id=3, alias="punk rock"))
    with DbWriter(create_keep_engine(tmp_path / "keep.db")) as writer:
        yield Env(engine, TagService(writer, TagTreeCache(engine)))
    engine.dispose()


OPERATIONS: dict[str, Callable[[TagService], object]] = {
    "add": lambda t: t.add(5, "Swing", "#112233"),
    "rename": lambda t: t.rename(3, "Punk Rock"),
    "reparent": lambda t: t.reparent(5, 7),
    "merge with clashing children": lambda t: t.merge(2, 10),
    "merge into ancestor": lambda t: t.merge(3, 1),
    "delete leaf": lambda t: t.delete(4),
    "delete subtree": lambda t: t.delete(1, DeleteMode.SUBTREE),
    "delete promoting children": lambda t: t.delete(5, DeleteMode.PROMOTE),
    "color": lambda t: t.set_color(8, "#abcdef"),
    "add alias": lambda t: t.add_alias(8, "chill"),
    "remove alias": lambda t: t.remove_alias(3, "PUNK ROCK"),
}


@pytest.mark.parametrize("op", OPERATIONS.values(), ids=OPERATIONS.keys())
def test_undo_restores_before_and_redo_restores_after(
    env: Env, op: Callable[[TagService], object]
) -> None:
    before = env.state()
    op(env.tags)
    after = env.state()
    assert after != before

    assert env.tags.undo() is not None
    assert env.state() == before
    assert env.tags.redo() is not None
    assert env.state() == after
    assert env.tags.undo() is not None
    assert env.state() == before


def test_a_sequence_undone_in_reverse_order(env: Env) -> None:
    # A chain where each step is valid after the previous ones.
    chain: list[Callable[[TagService], object]] = [
        lambda t: t.add(5, "Swing"),
        lambda t: t.set_color(8, "#abcdef"),
        lambda t: t.add_alias(8, "chill"),
        lambda t: t.rename(4, "Classics"),
        lambda t: t.reparent(5, 7),
        lambda t: t.merge(2, 10),  # Punk (3) merges into Music/Rock/Punk (11)
        lambda t: t.remove_alias(11, "punk rock"),
        lambda t: t.delete(5, DeleteMode.PROMOTE),
        lambda t: t.delete(9, DeleteMode.SUBTREE),
    ]
    states = [env.state()]
    for op in chain:
        op(env.tags)
        states.append(env.state())
    for expected in reversed(states[:-1]):
        env.tags.undo()
        assert env.state() == expected
    assert env.tags.undo() is None
    for expected in states[1:]:
        env.tags.redo()
        assert env.state() == expected
    assert env.tags.redo() is None


def test_cache_follows_undo_and_redo(env: Env) -> None:
    env.tags.rename(3, "Punk Rock")
    env.tags.undo()
    assert env.tags.cache.get().node(3).name == "Punk"
    env.tags.redo()
    assert env.tags.cache.get().node(3).name == "Punk Rock"


def test_labels(env: Env) -> None:
    env.tags.merge(6, 5)
    assert env.tags.undo_label == "Merge 'Bebop' into 'Jazz'"
    env.tags.rename(5, "Jazz & Blues")
    assert env.tags.undo_label == "Rename 'Jazz' to 'Jazz & Blues'"
    assert env.tags.undo() == "Rename 'Jazz' to 'Jazz & Blues'"
    assert env.tags.redo_label == "Rename 'Jazz' to 'Jazz & Blues'"
    assert env.tags.undo_label == "Merge 'Bebop' into 'Jazz'"


def test_a_new_operation_clears_redo(env: Env) -> None:
    env.tags.rename(3, "A")
    env.tags.undo()
    env.tags.rename(3, "B")
    assert env.tags.redo() is None


def test_refused_operations_are_not_recorded(env: Env) -> None:
    before = env.state()
    with pytest.raises(TagError):
        env.tags.add(None, "genre")
    assert env.tags.undo_label is None
    assert env.state() == before


def test_undo_keeps_tagging_done_after_the_operation(env: Env) -> None:
    env.tags.merge(6, 5)  # Bebop into Jazz: entity 103 gets Jazz
    with env.engine.begin() as conn:  # the user then tags entity 100 with Jazz
        conn.execute(insert(EntityTag).values(entity_id=100, tag_id=5))
    env.tags.undo()
    with env.engine.connect() as conn:
        tags_of_100 = set(conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id == 100)))
        tags_of_103 = set(conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id == 103)))
    assert tags_of_100 == {3, 5}  # kept: not part of the merge
    assert tags_of_103 == {6, 8}  # the merge's own change was reversed


def test_undo_skips_entities_deleted_since(env: Env) -> None:
    env.tags.delete(4)  # entity 102 loses Classic Rock
    with env.engine.begin() as conn:
        conn.execute(delete(Entity).where(Entity.id == 102))
    env.tags.undo()
    with env.engine.connect() as conn:
        assert conn.scalar(select(Tag.name).where(Tag.id == 4)) == "Classic Rock"
        assert not list(conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id == 102)))


def test_history_is_bounded(env: Env) -> None:
    for i in range(MAX_HISTORY + 5):
        env.tags.set_color(8, f"#{i:06x}")
    undone = 0
    while env.tags.undo() is not None:
        undone += 1
    assert undone == MAX_HISTORY


def test_clear_history(env: Env) -> None:
    env.tags.rename(3, "A")
    env.tags.clear_history()
    assert env.tags.undo() is None
