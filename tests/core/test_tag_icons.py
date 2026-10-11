"""Tag icons, shown as badges on items (#398, DESIGN.md §7)."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, select

from tagalot.builtin_themes.books import BooksTheme
from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Tag
from tagalot.core.tag_service import TagService
from tagalot.core.tag_yaml import TagUpdate, export_tags, plan_import
from tagalot.core.tags import (
    Badge,
    TagError,
    TagNode,
    TagTree,
    TagTreeCache,
    clean_icon,
)
from tagalot.core.theme_schema import build_theme_schema
from tagalot.core.writer import DbWriter

SCHEMA = build_theme_schema(BooksTheme)

VALID = {
    "a star": "⭐",
    "a check": "✓",
    "a letter": "A",
    "an accented letter, decomposed": "é",
    "a skin tone": "\U0001f44d\U0001f3fd",
    "a joined family": "\U0001f468‍\U0001f469‍\U0001f467",
    "a flag": "\U0001f1ee\U0001f1f8",
    "a keycap": "1️⃣",
    "a heart with its variation": "❤️",
    "a subdivision flag": "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
}


@pytest.mark.parametrize("icon", VALID.values(), ids=VALID.keys())
def test_one_emoji_or_character_is_an_icon(icon: str) -> None:
    assert clean_icon(f"  {icon} ") == icon


INVALID = {
    "two letters": "ab",
    "two stars": "⭐⭐",
    "with a space": "a b",
    "two flags": "\U0001f1ee\U0001f1f8\U0001f1f3\U0001f1f4",
    "a dangling joiner": "\U0001f44d‍",
    "too long": "⭐" + "️" * 20,
}


@pytest.mark.parametrize("icon", INVALID.values(), ids=INVALID.keys())
def test_more_than_one_character_is_refused(icon: str) -> None:
    with pytest.raises(TagError, match="one emoji or one character"):
        clean_icon(icon)


def test_blank_clears() -> None:
    assert clean_icon("  ") is None
    assert clean_icon(None) is None


# Places (globe) > Iceland, Beach (shell) ; People > Family ; Favorites (star) > Top ;
# Read (check) > Finished, Abandoned.
PLACES, ICELAND, BEACH, PEOPLE, FAMILY, FAVORITES, TOP, READ, FINISHED, ABANDONED = range(1, 11)
GLOBE, SHELL, STAR, CHECK = "\U0001f30d", "\U0001f41a", "⭐", "✓"


def _tree() -> TagTree:
    return TagTree(
        [
            TagNode(PLACES, None, "Places", None, 0, icon=GLOBE),
            TagNode(ICELAND, PLACES, "Iceland", None, 0),
            TagNode(BEACH, PLACES, "Beach", None, 1, icon=SHELL),
            TagNode(PEOPLE, None, "People", None, 1),
            TagNode(FAMILY, PEOPLE, "Family", None, 0),
            TagNode(FAVORITES, None, "Favorites", None, 2, icon=STAR),
            TagNode(TOP, FAVORITES, "Top", None, 0),
            TagNode(READ, None, "Read", None, 3, icon=CHECK),
            TagNode(FINISHED, READ, "Finished", None, 0),
            TagNode(ABANDONED, READ, "Abandoned", None, 1),
        ],
        {},
    )


def test_a_sub_tag_shows_its_nearest_ancestors_icon() -> None:
    tree = _tree()
    assert tree.icon(ICELAND) == GLOBE  # from Places
    assert tree.icon(BEACH) == SHELL  # its own
    assert tree.icon(FAMILY) is None
    assert tree.has_icons
    assert not TagTree([TagNode(1, None, "Plain", None, 0)], {}).has_icons


def test_badges_show_each_icon_once_in_name_order() -> None:
    tree = _tree()
    badges = tree.badges([FINISHED, ABANDONED, ICELAND, FAMILY, FAVORITES, TOP, 999])
    assert badges == (
        Badge(STAR, (FAVORITES, TOP)),  # Favorites before Places, before Read
        Badge(GLOBE, (ICELAND,)),
        Badge(CHECK, (ABANDONED, FINISHED)),  # two sub-tags of Read: one badge
    )
    assert tree.badges([FAMILY]) == ()  # no icon anywhere above it
    assert tree.badges([]) == ()


@dataclass
class Env:
    engine: Engine
    tags: TagService

    def icons(self) -> dict[str, str | None]:
        with self.engine.connect() as conn:
            return {n: i for n, i in conn.execute(select(Tag.name, Tag.icon))}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(Tag),
            [
                {"id": 1, "parent_id": None, "name": "Favorites", "sort_order": 0, "icon": STAR},
                {"id": 2, "parent_id": None, "name": "Best", "sort_order": 1, "icon": None},
                {"id": 3, "parent_id": None, "name": "Read", "sort_order": 2, "icon": None},
            ],
        )
    with DbWriter(create_keep_engine(tmp_path / "keep.db")) as writer:
        yield Env(engine, TagService(writer, TagTreeCache(engine), SCHEMA))
    engine.dispose()


def test_setting_an_icon_is_one_undoable_step(env: Env) -> None:
    env.tags.set_icon(3, CHECK)
    assert env.icons()["Read"] == CHECK
    assert env.tags.undo_label == "Change the icon of 'Read'"
    env.tags.set_icon(3, None)
    assert env.icons()["Read"] is None
    env.tags.undo()
    assert env.icons()["Read"] == CHECK
    env.tags.undo()
    assert env.icons()["Read"] is None
    env.tags.redo()
    assert env.icons()["Read"] == CHECK
    with pytest.raises(TagError):
        env.tags.set_icon(3, "ok")
    assert env.icons()["Read"] == CHECK


def test_a_merge_gives_the_target_the_sources_icon_if_it_has_none(env: Env) -> None:
    env.tags.merge(1, 2)  # Favorites into Best
    assert env.icons() == {"Best": STAR, "Read": None}


def test_export_and_import_carry_icons(env: Env) -> None:
    with env.engine.connect() as conn:
        text = export_tags(TagTree.load(conn), SCHEMA)
        assert f"Favorites:\n  icon: {STAR}\n" in text
        plan = plan_import(conn, f"Read:\n  icon: '{CHECK}'\nBest:\n  icon: no\n", SCHEMA)
    assert plan.updated == (TagUpdate("Read", ("icon",)),)
    [problem] = plan.problems
    assert problem.line == 4
    assert "one emoji or one character" in problem.message
    env.tags.import_tags(f"Read:\n  icon: '{CHECK}'\nNew:\n  icon: {STAR}\n", "tags.yaml")
    assert env.icons() == {"Favorites": STAR, "Best": None, "Read": CHECK, "New": STAR}
