"""Tags limited to some item types (#135, DESIGN.md §7 "Tag types").

A tag's types are inherited by its sub-tags, which can only narrow them. Tagging skips
items a tag doesn't allow (and says which); limiting a tag takes it off items of other
types, as one undoable step.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, select

from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Entity, EntityTag, Tag
from tagalot.core.tag_service import TagService
from tagalot.core.tags import (
    TagError,
    TagNode,
    TagTree,
    TagTreeCache,
    format_types,
    parse_types,
    scope_conflicts,
)
from tagalot.core.writer import DbWriter

GENRE, FANTASY, SUPERHERO, FAVORITES = 1, 2, 3, 4
BOOK, COMIC, AUTHOR = "books.book", "books.comic", "books.author"
# Two books (1, 2), a comic (3), and an author (4).
ENTITIES = {1: BOOK, 2: BOOK, 3: COMIC, 4: AUTHOR}


def _tree(genre: str | None = None, superhero: str | None = None) -> TagTree:
    return TagTree(
        [
            TagNode(GENRE, None, "Genre", None, 0, types=genre),
            TagNode(FANTASY, GENRE, "Fantasy", None, 0),
            TagNode(SUPERHERO, GENRE, "Superhero", None, 1, types=superhero),
            TagNode(FAVORITES, None, "Favorites", None, 1),
        ],
        {},
    )


def test_types_are_stored_sorted_and_parsed_back() -> None:
    assert format_types([COMIC, BOOK, BOOK]) == "books.book books.comic"
    assert format_types(None) is None
    assert format_types([]) is None
    assert parse_types("books.book books.comic") == frozenset({BOOK, COMIC})
    assert parse_types(None) is None
    assert parse_types("  ") is None


def test_sub_tags_inherit_and_can_only_narrow() -> None:
    tree = _tree(genre=f"{BOOK} {COMIC}", superhero=f"{COMIC} {AUTHOR}")
    assert tree.scope(GENRE) == frozenset({BOOK, COMIC})
    assert tree.scope(FANTASY) == frozenset({BOOK, COMIC})  # inherited
    assert tree.scope(SUPERHERO) == frozenset({COMIC})  # narrowed: the author type isn't Genre's
    assert tree.scope(FAVORITES) is None  # every type
    assert tree.allows(FANTASY, BOOK)
    assert not tree.allows(FANTASY, AUTHOR)
    assert tree.allows(FAVORITES, AUTHOR)
    assert tree.with_types(GENRE, None).scope(SUPERHERO) == frozenset({COMIC, AUTHOR})


@dataclass
class Env:
    engine: Engine
    tags: TagService

    def links(self) -> set[tuple[int, int]]:
        with self.engine.connect() as conn:
            return {(e, t) for e, t in conn.execute(select(EntityTag.entity_id, EntityTag.tag_id))}

    def types(self, tag_id: int) -> str | None:
        with self.engine.connect() as conn:
            return conn.scalar(select(Tag.types).where(Tag.id == tag_id))


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(Tag),
            [
                {"id": GENRE, "parent_id": None, "name": "Genre"},
                {"id": FANTASY, "parent_id": GENRE, "name": "Fantasy"},
                {"id": SUPERHERO, "parent_id": GENRE, "name": "Superhero"},
                {"id": FAVORITES, "parent_id": None, "name": "Favorites"},
            ],
        )
        conn.execute(
            insert(Entity), [{"id": e, "type": t, "title": f"E{e}"} for e, t in ENTITIES.items()]
        )
    with DbWriter(create_keep_engine(tmp_path / "keep.db")) as writer:
        yield Env(engine, TagService(writer, TagTreeCache(engine)))
    engine.dispose()


def test_tagging_skips_what_a_tag_doesnt_allow(env: Env) -> None:
    assert env.tags.set_types(GENRE, [BOOK, COMIC]) == 0
    applied = env.tags.apply_counted([1, 3, 4], [FANTASY, FAVORITES])
    assert applied.added == 5  # Fantasy on the book and the comic; Favorites on all three
    assert applied.skipped == ((4, FANTASY, AUTHOR),)
    assert env.links() == {
        (1, FANTASY),
        (3, FANTASY),
        (1, FAVORITES),
        (3, FAVORITES),
        (4, FAVORITES),
    }
    assert env.tags.undo_label == "Tag 3 items with 'Fantasy', 'Favorites'"


def test_limiting_a_tag_takes_it_off_other_types_and_undoes(env: Env) -> None:
    env.tags.apply([1, 3, 4], [FANTASY])
    env.tags.apply([4], [GENRE])
    with env.engine.connect() as conn:
        tree = TagTree.load(conn).with_types(GENRE, [BOOK])
        assert sorted(scope_conflicts(conn, tree, GENRE)) == [
            (3, FANTASY),
            (4, GENRE),
            (4, FANTASY),
        ]

    assert env.tags.set_types(GENRE, [BOOK]) == 3
    assert env.types(GENRE) == BOOK
    assert env.links() == {(1, FANTASY)}
    assert env.tags.undo_label == "Change the types 'Genre' applies to"

    env.tags.undo()
    assert env.types(GENRE) is None
    assert env.links() == {(1, FANTASY), (3, FANTASY), (4, FANTASY), (4, GENRE)}
    env.tags.redo()
    assert env.types(GENRE) == BOOK
    assert env.links() == {(1, FANTASY)}

    assert env.tags.set_types(GENRE, None) == 0  # every type again; nothing comes back
    assert env.types(GENRE) is None
    assert env.links() == {(1, FANTASY)}


def test_a_sub_tag_narrows_its_parent(env: Env) -> None:
    env.tags.set_types(GENRE, [BOOK, COMIC])
    env.tags.apply([1, 3], [SUPERHERO])
    assert env.tags.set_types(SUPERHERO, [COMIC, AUTHOR]) == 1  # the book loses it
    assert env.links() == {(3, SUPERHERO)}
    applied = env.tags.apply_counted([4], [SUPERHERO])
    assert applied.skipped == ((4, SUPERHERO, AUTHOR),)  # authors aren't Genre's


def test_no_types_at_all_is_refused(env: Env) -> None:
    with pytest.raises(TagError, match="at least one type"):
        env.tags.set_types(GENRE, [])
    assert env.types(GENRE) is None
