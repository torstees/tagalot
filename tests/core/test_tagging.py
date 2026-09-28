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
from tagalot.core.tags import (
    LINK_BATCH,
    PATH_SEPARATOR,
    TagError,
    TagTreeCache,
    TagUsage,
    split_tag_path,
    tag_counts,
    tag_usage,
)
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


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("Norway", ["Norway"]),
        ("  Places  >  Norway ", ["Places", "Norway"]),
        (f"Places{PATH_SEPARATOR}Norway", ["Places", "Norway"]),
        (f"Places{PATH_SEPARATOR.strip()}Norway", ["Places", "Norway"]),  # no spaces
        ("AC/DC", ["AC/DC"]),  # "/" is part of names
        ("5>3", ["5>3"]),  # ">" splits only with spaces around it
        ("Music > Rock > Punk", ["Music", "Rock", "Punk"]),
    ],
)
def test_split_tag_path(text: str, names: list[str]) -> None:
    assert split_tag_path(text) == names


def test_split_tag_path_refuses_an_empty_level() -> None:
    with pytest.raises(TagError, match="empty"):
        split_tag_path(f"Places{PATH_SEPARATOR}{PATH_SEPARATOR}Norway")


def test_add_path_under_an_existing_tag_and_with_new_parents(env: Env) -> None:
    norway = env.tags.add_path(["places", "Norway"])  # matches Places ignoring case
    tree = env.tags.cache.get()
    assert tree.node(norway).parent_id == PLACES
    assert env.tags.undo_label == f"Add tag 'places{PATH_SEPARATOR}Norway'"

    punk = env.tags.add_path(["Music", "Rock", "Punk"])  # all three are new
    tree = env.tags.cache.get()
    assert tree.path(punk) == ("Music", "Rock", "Punk")
    env.tags.undo()  # one step removes all three
    assert "Music" not in {tree.node(t).name for t in env.tags.cache.get().children(None)}

    assert env.tags.add_path(["Places", "Iceland"]) == ICELAND  # already there: no new tag


def test_tag_usage(env: Env) -> None:
    env.tags.apply([1, 2], [BEACH])  # entity 1 also has Iceland: counted once under Places
    env.tags.apply([3], [PLACES])  # a parent applied directly
    tree = env.tags.cache.get()
    with env.engine.connect() as conn:
        usage = tag_usage(conn, tree)
    assert usage[ICELAND] == TagUsage(1, 1)
    assert usage[BEACH] == TagUsage(2, 2)
    assert usage[PLACES] == TagUsage(1, 3)  # entity 3 directly; 1 and 2 through sub-tags
    assert usage[FAVORITES] == TagUsage(0, 0)
    assert set(usage) == set(tree)
