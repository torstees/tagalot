"""Tests for applying and removing tags on entities, with undo (DESIGN.md §7, §12)."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, select

from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Entity, EntityTag, Tag
from tagalot.core.tag_service import TagService
from tagalot.core.tags import LINK_BATCH, TagError, TagTreeCache, tag_counts
from tagalot.core.writer import DbWriter

PLACES, ICELAND, BEACH, FAVORITES = 1, 2, 3, 4
TAGS = [(PLACES, None, "Places"), (ICELAND, PLACES, "Iceland"), (BEACH, PLACES, "Beach")]
TAGS.append((FAVORITES, None, "Favorites"))
EARLIER = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class Env:
    engine: Engine
    tags: TagService

    def links(self) -> dict[tuple[int, int], datetime]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(EntityTag.entity_id, EntityTag.tag_id, EntityTag.added_at))
            return {(e, t): a for e, t, a in rows}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(Tag), [{"id": i, "parent_id": p, "name": n} for i, p, n in TAGS])
        conn.execute(
            insert(Entity), [{"id": e, "type": "t", "title": f"E{e}"} for e in range(1, 2001)]
        )
        conn.execute(insert(EntityTag).values(entity_id=1, tag_id=ICELAND, added_at=EARLIER))
    with DbWriter(create_keep_engine(tmp_path / "keep.db")) as writer:
        yield Env(engine, TagService(writer, TagTreeCache(engine)))
    engine.dispose()


def test_apply_adds_only_missing_pairs(env: Env) -> None:
    assert env.tags.apply([1, 2, 3], [ICELAND]) == 2
    links = env.links()
    assert set(links) == {(1, ICELAND), (2, ICELAND), (3, ICELAND)}
    assert links[1, ICELAND] == EARLIER  # already tagged: untouched
    assert (1, PLACES) not in links  # parents are never added (§7)


def test_apply_several_tags_skips_missing_entities(env: Env) -> None:
    assert env.tags.apply([5, 9999], [BEACH, FAVORITES]) == 2
    assert {k for k in env.links() if k[0] == 5} == {(5, BEACH), (5, FAVORITES)}


def test_an_unknown_tag_is_an_error(env: Env) -> None:
    with pytest.raises(TagError, match="no longer exists"):
        env.tags.apply([1], [ICELAND, 999])
    assert set(env.links()) == {(1, ICELAND)}  # all or nothing
    assert env.tags.undo_label is None


def test_remove_removes_exactly_those_tags(env: Env) -> None:
    env.tags.apply([1, 2], [BEACH])
    assert env.tags.remove([1, 2, 3], [ICELAND, FAVORITES]) == 1  # only 1 had Iceland
    assert set(env.links()) == {(1, BEACH), (2, BEACH)}
    assert env.tags.remove([1], [PLACES]) == 0  # a parent: its children stay


def test_large_selections_are_batched(env: Env) -> None:
    many = range(1, 2001)
    assert LINK_BATCH < 2000
    assert env.tags.apply(many, [FAVORITES]) == 2000
    assert env.tags.remove(many, [FAVORITES]) == 2000
    assert set(env.links()) == {(1, ICELAND)}


def test_undo_and_redo_tagging(env: Env) -> None:
    env.tags.apply([1, 2], [ICELAND])
    after = env.links()
    assert env.tags.undo_label == "Tag 2 items with 'Iceland'"
    assert env.tags.undo() == "Tag 2 items with 'Iceland'"
    assert env.links() == {(1, ICELAND): EARLIER}  # the earlier tag stays
    assert env.tags.redo() == "Tag 2 items with 'Iceland'"
    assert env.links() == after


def test_undo_restores_removed_tags_with_their_dates(env: Env) -> None:
    env.tags.remove([1], [ICELAND])
    assert env.tags.undo_label == "Remove 'Iceland' from 1 item"
    env.tags.undo()
    assert env.links() == {(1, ICELAND): EARLIER}


def test_a_drop_that_changes_nothing_is_not_an_undo_step(env: Env) -> None:
    assert env.tags.apply([1], [ICELAND]) == 0
    assert env.tags.remove([2], [BEACH]) == 0
    assert env.tags.undo_label is None


def test_labels_name_up_to_three_tags(env: Env) -> None:
    env.tags.apply([7], [ICELAND, BEACH, FAVORITES])
    assert env.tags.undo_label == "Tag 1 item with 'Beach', 'Favorites', 'Iceland'"
    env.tags.apply(range(10, 1210), [PLACES, ICELAND, BEACH, FAVORITES])
    assert env.tags.undo_label == "Tag 1,200 items with 4 tags"


def test_tag_counts(env: Env) -> None:
    env.tags.apply([1, 2, 3], [FAVORITES])
    env.tags.apply([2], [BEACH])
    with env.engine.connect() as conn:
        assert tag_counts(conn, [1, 2, 3, 4]) == {ICELAND: 1, FAVORITES: 3, BEACH: 1}
        assert tag_counts(conn, [4]) == {}
        assert tag_counts(conn, range(1, 2001))[FAVORITES] == 3  # across batches
