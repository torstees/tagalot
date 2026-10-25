"""Tag groups (#534, DESIGN.md §7 *Tag groups*): named groups of tags kept in the keep, each
tag bringing its sub-tags along; every change an undoable step; deleting and merging tags
keep them right."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert

from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Tag
from tagalot.core.tag_service import TagService
from tagalot.core.tags import DeleteMode, TagError, TagTree, TagTreeCache
from tagalot.core.writer import DbWriter

TAGS = [
    (1, None, "Places"),
    (2, 1, "Europe"),
    (3, 2, "Norway"),
    (4, 3, "Oslo"),
    (5, 2, "Iceland"),
    (6, None, "People"),
    (7, 6, "Family"),
    (8, 7, "Anna"),
    (9, None, "Norge"),
]


@dataclass
class Env:
    engine: Engine
    tags: TagService

    def tree(self) -> TagTree:
        with self.engine.connect() as conn:
            return TagTree.load(conn)

    def groups(self) -> dict[str, set[str]]:
        tree = self.tree()
        return {g.name: {tree.node(t).name for t in g.tags} for g in tree.groups()}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(Tag), [{"id": i, "parent_id": p, "name": n} for i, p, n in TAGS])
    with DbWriter(create_keep_engine(tmp_path / "keep.db")) as writer:
        yield Env(engine, TagService(writer, TagTreeCache(engine)))
    engine.dispose()


def test_groups_hold_tags_and_are_listed_in_order(env: Env) -> None:
    trips = env.tags.add_group("  Trips ")
    family = env.tags.add_group("Family photos")
    assert [g.name for g in env.tree().groups()] == ["Trips", "Family photos"]  # as made
    assert env.tags.add_to_group(trips, [3, 5, 4, 999]) == 3  # a tag that isn't is left out
    assert env.tags.add_to_group(trips, [3]) == 0
    env.tags.add_to_group(family, [7])
    tree = env.tree()
    assert env.groups() == {"Trips": {"Norway", "Iceland", "Oslo"}, "Family photos": {"Family"}}
    assert tree.groups_of(3) == (trips,)
    assert tree.groups_of(8) == ()  # Anna is in Family photos through Family, not itself
    # The heading's tags: those not under another of the group's, in tree order.
    assert [tree.node(t).name for t in tree.group_tops(trips)] == ["Iceland", "Norway"]
    assert tree.find_group("TRIPS") == trips

    with pytest.raises(TagError, match="already a group named 'Trips'"):
        env.tags.add_group("trips")
    with pytest.raises(TagError, match="needs a name"):
        env.tags.add_group("   ")
    env.tags.rename_group(trips, "Travel")
    env.tags.rename_group(trips, "travel")  # its own name, in another case
    assert env.tags.remove_from_group(trips, [5, 7]) == 1
    assert env.groups() == {"travel": {"Norway", "Oslo"}, "Family photos": {"Family"}}
    env.tags.delete_group(family)
    assert env.groups() == {"travel": {"Norway", "Oslo"}}
    assert 7 in env.tree()  # its tags stay
    with pytest.raises(TagError, match="no longer exists"):
        env.tags.rename_group(family, "Again")


def test_every_change_is_an_undo_step(env: Env) -> None:
    trips = env.tags.add_group("Trips")
    env.tags.add_to_group(trips, [3, 5])
    env.tags.rename_group(trips, "Travel")
    env.tags.delete_group(trips)
    assert env.groups() == {}
    assert env.tags.undo() == "Delete group 'Travel'"
    assert env.groups() == {"Travel": {"Norway", "Iceland"}}
    assert env.tags.undo() == "Rename group 'Trips' to 'Travel'"
    assert env.tags.undo() == "Add 'Iceland', 'Norway' to group 'Trips'"
    assert env.groups() == {"Trips": set()}
    assert env.tags.undo() == "Add group 'Trips'"
    assert env.groups() == {}
    for _ in range(4):
        env.tags.redo()
    assert env.groups() == {}
    env.tags.undo()
    assert env.tree().groups()[0].id == trips  # the same group back, by id

    env.tags.remove_from_group(trips, [5])
    assert env.tags.undo() == "Take 'Iceland' out of group 'Travel'"
    assert env.groups() == {"Travel": {"Norway", "Iceland"}}


def test_deleting_and_merging_tags_keep_groups_right(env: Env) -> None:
    trips = env.tags.add_group("Trips")
    env.tags.add_to_group(trips, [3, 4])
    env.tags.delete(3, DeleteMode.SUBTREE)  # Norway and Oslo
    assert env.groups() == {"Trips": set()}
    env.tags.undo()  # they come back in the group
    assert env.groups() == {"Trips": {"Norway", "Oslo"}}

    env.tags.merge(3, 9)  # Norway into Norge: Norge joins Norway's groups
    assert env.groups() == {"Trips": {"Norge", "Oslo"}}
    env.tags.undo()
    assert env.groups() == {"Trips": {"Norway", "Oslo"}}

    env.tags.apply([], [3])  # tagging leaves groups alone, and so does undoing it
    env.tags.rename(3, "Noreg")
    env.tags.undo()
    assert env.groups() == {"Trips": {"Norway", "Oslo"}}
