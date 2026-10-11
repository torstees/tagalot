"""Importing and exporting the tag tree as YAML (#395, DESIGN.md §7)."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select

from tagalot.builtin_themes.books import BooksTheme
from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Entity, EntityTag, Tag, TagAlias
from tagalot.core.tag_service import TagService
from tagalot.core.tag_yaml import ImportPlan, TagUpdate, export_tags, plan_import
from tagalot.core.tags import PATH_SEPARATOR, TagError, TagTree, TagTreeCache
from tagalot.core.theme_schema import ThemeSchema, build_theme_schema
from tagalot.core.writer import DbWriter

SCHEMA: ThemeSchema = build_theme_schema(BooksTheme)

# Genre (Books, Comics) > Fantasy, Science Fiction (aliases SF, Sci-Fi; blue); Mood > Calm;
# Read; Places > Ísland. Book 1 has Fantasy, book 2 has Read.
TAGS: list[dict[str, Any]] = [
    {"id": 1, "parent_id": None, "name": "Genre", "types": "books.book books.comic"},
    {"id": 2, "parent_id": 1, "name": "Fantasy", "sort_order": 0},
    {"id": 3, "parent_id": 1, "name": "Science Fiction", "color": "#4a7bd0", "sort_order": 1},
    {"id": 4, "parent_id": None, "name": "Mood", "sort_order": 1},
    {"id": 5, "parent_id": 4, "name": "Calm"},
    {"id": 6, "parent_id": None, "name": "Read", "sort_order": 2},
    {"id": 7, "parent_id": None, "name": "Places", "sort_order": 3},
    {"id": 8, "parent_id": 7, "name": "Ísland", "description": "Summer 2019.\nTwo weeks."},
]


def p(*names: str) -> str:
    return PATH_SEPARATOR.join(names)


@dataclass
class Env:
    engine: Engine
    tags: TagService

    def plan(self, text: str, delete_unused: bool = False) -> ImportPlan:
        with self.engine.connect() as conn:
            return plan_import(conn, text, SCHEMA, delete_unused)

    def tree(self) -> TagTree:
        with self.engine.connect() as conn:
            return TagTree.load(conn)

    def export(self) -> str:
        return export_tags(self.tree(), SCHEMA)

    def state(self) -> tuple[frozenset[tuple[object, ...]], ...]:
        queries = [
            select(Tag.id, Tag.parent_id, Tag.name, Tag.color, Tag.description, Tag.types),
            select(TagAlias.tag_id, TagAlias.alias),
            select(EntityTag.entity_id, EntityTag.tag_id),
        ]
        with self.engine.connect() as conn:
            return tuple(frozenset(tuple(r) for r in conn.execute(q)) for q in queries)

    def find(self, *names: str) -> int:
        tree = self.tree()
        tag_id = None
        for name in names:
            tag_id = tree.find_child(tag_id, name)
            assert tag_id is not None, names
        assert tag_id is not None
        return tag_id


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        defaults = {"sort_order": 0, "color": None, "description": None, "types": None}
        conn.execute(insert(Tag), [{**defaults, **t} for t in TAGS])
        conn.execute(
            insert(TagAlias), [{"tag_id": 3, "alias": "SF"}, {"tag_id": 3, "alias": "Sci-Fi"}]
        )
        conn.execute(
            insert(Entity), [{"id": e, "type": "books.book", "title": f"B{e}"} for e in (1, 2)]
        )
        conn.execute(
            insert(EntityTag), [{"entity_id": 1, "tag_id": 2}, {"entity_id": 2, "tag_id": 6}]
        )
    with DbWriter(create_keep_engine(tmp_path / "keep.db")) as writer:
        yield Env(engine, TagService(writer, TagTreeCache(engine), SCHEMA))
    engine.dispose()


EXPORTED = """\
# Tagalot tags. Edit this file and load it back with Tag manager → Import tags…
Genre:
  applies_to: [Book, Comic]
  tags:
    Fantasy:
    Science Fiction:
      color: '#4a7bd0'
      aliases: [Sci-Fi, SF]
Mood:
  tags:
    Calm:
Read:
Places:
  tags:
    Ísland:
      description: |-
        Summer 2019.
        Two weeks.
"""


def test_export_writes_the_full_form(env: Env) -> None:
    assert env.export() == EXPORTED


def test_a_round_trip_changes_nothing(env: Env) -> None:
    before = env.state()
    plan = env.plan(EXPORTED, delete_unused=True)
    assert plan == ImportPlan()
    env.tags.import_tags(EXPORTED, "tags.yaml", delete_unused=True)
    assert env.state() == before


def test_an_empty_tree_round_trips(tmp_path: Path) -> None:
    empty = export_tags(TagTree([], {}), SCHEMA)
    engine = create_keep_engine(tmp_path / "empty.db")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        assert plan_import(conn, empty, SCHEMA) == ImportPlan()
    engine.dispose()


def test_shorthand_and_full_form_create_tags(env: Env) -> None:
    text = """
Mood: [Calm, Tense]
Places:
  tags:
    Norway: [Oslo, Bergen]
    Japan:
      color: "#C0392B"
      description: Spring
      aliases: Nippon
      tags:
        Tokyo:
Format:
"""
    plan = env.plan(text)
    assert plan.new == (
        p("Mood", "Tense"),
        p("Places", "Norway"),
        p("Places", "Norway", "Oslo"),
        p("Places", "Norway", "Bergen"),
        p("Places", "Japan"),
        p("Places", "Japan", "Tokyo"),
        "Format",
    )
    assert plan.updated == ()
    assert plan.problems == ()
    env.tags.import_tags(text, "tags.yaml")
    tree = env.tree()
    japan = env.find("Places", "Japan")
    assert tree.node(japan).color == "#c0392b"
    assert tree.node(japan).description == "Spring"
    assert tree.aliases(japan) == ("Nippon",)
    oslo, bergen = env.find("Places", "Norway", "Oslo"), env.find("Places", "Norway", "Bergen")
    assert tree.children(env.find("Places", "Norway")) == (oslo, bergen)  # in file order
    assert tree.children(None)[-1] == env.find("Format")  # after the existing top level


def test_matching_finds_aliases_without_renaming(env: Env) -> None:
    text = """
Genre:
  tags:
    sci-fi:
      color: "#112233"
"""
    plan = env.plan(text)
    assert plan.new == ()
    assert plan.updated == (TagUpdate(p("Genre", "Science Fiction"), ("color",)),)
    env.tags.import_tags(text, "tags.yaml")
    tree = env.tree()
    assert tree.node(3).color == "#112233"
    assert tree.node(3).name == "Science Fiction"  # an alias never renames
    assert len(tree) == len(TAGS)


def test_a_name_spelled_differently_renames_the_tag(env: Env) -> None:
    """Matching ignores case and accents; the file's spelling then becomes the name, so
    small fixes can be made in a text editor."""
    text = """
genre:
  tags:
    Science fiction:
      color: "#112233"
Places: [Island]
"""
    plan = env.plan(text)
    assert plan.new == ()
    assert plan.updated == (
        TagUpdate("Genre", ("renamed to 'genre'",)),
        TagUpdate(p("genre", "Science Fiction"), ("renamed to 'Science fiction'", "color")),
        TagUpdate(p("Places", "\u00cdsland"), ("renamed to 'Island'",)),
    )
    env.tags.import_tags(text, "tags.yaml")
    tree = env.tree()
    assert [tree.node(t).name for t in (1, 3, 8)] == ["genre", "Science fiction", "Island"]
    assert tree.node(3).color == "#112233"
    assert len(tree) == len(TAGS)
    assert env.plan(text) == ImportPlan()  # done


def test_a_rename_drops_an_alias_that_is_now_the_name(env: Env) -> None:
    env.tags.add_alias(8, "Island")
    env.tags.import_tags("Places: [Island]\n", "tags.yaml")
    tree = env.tree()
    assert tree.node(8).name == "Island"
    assert tree.aliases(8) == ()


def test_properties_merge_into_what_is_there(env: Env) -> None:
    text = """
Genre:
  applies_to: [Books]
  tags:
    Science Fiction:
      description: Rockets
      aliases: [sf, Space Opera]
Places: [Ísland]
"""
    plan = env.plan(text)
    assert plan.updated == (
        TagUpdate("Genre", ("applies to",)),
        TagUpdate(p("Genre", "Science Fiction"), ("description", "aliases")),
    )
    env.tags.import_tags(text, "tags.yaml")
    tree = env.tree()
    sf = tree.node(3)
    assert (sf.color, sf.description) == ("#4a7bd0", "Rockets")  # the color stays
    assert sorted(tree.aliases(3)) == ["SF", "Sci-Fi", "Space Opera"]  # added to
    assert tree.node(1).types == "books.book"
    assert tree.node(8).description == "Summer 2019.\nTwo weeks."  # short form: left alone


PROBLEMS = {
    "not yaml": ("Genre: [Fantasy\n", 2, "This isn't YAML"),
    "bad color": ("Read:\n  color: blue\n", 2, "'blue' isn't a color"),
    "unknown kind": ("Read:\n  applies_to: [Book, Film]\n", 2, "'Film', which isn't a kind"),
    "a mapping in a short list": ("Mood:\n  - Calm\n  - Tense: [Very]\n", 3, "only names"),
    "sub-tags not under tags": ("Mood:\n  Calm:\n", 2, "'Calm' isn't a tag property"),
    "wider than its parent": (
        "Genre:\n  tags:\n    Fantasy:\n      applies_to: [Comic, Author]\n",
        3,
        "'Fantasy' applies to Authors, which 'Genre' doesn't",
    ),
    "twice": ("Mood: [Calm, calm]\n", 1, "'calm' is here twice"),
    "two names, one tag": (
        "Genre:\n  tags:\n    SF:\n    Sci-Fi:\n      color: '#000000'\n",
        4,
        "names the same tag as another one here",
    ),
    "duplicate keys": ("Read:\nRead:\n", 2, "This isn't YAML"),
}


@pytest.mark.parametrize("case", PROBLEMS.values(), ids=PROBLEMS.keys())
def test_problems_are_reported_with_their_line(env: Env, case: tuple[str, int, str]) -> None:
    text, line, message = case
    plan = env.plan(text)
    assert len(plan.problems) == 1, plan.problems
    [problem] = plan.problems
    assert problem.line == line
    assert message in problem.message, problem.message
    assert str(problem).startswith(f"Line {line}: ")


def test_the_rest_of_a_file_with_problems_still_imports(env: Env) -> None:
    text = """
Read:
  color: blue
  description: Done
Genre:
  tags:
    Fantasy:
      applies_to: [Comic, Author]
"""
    env.tags.import_tags(text, "tags.yaml")
    tree = env.tree()
    assert (tree.node(6).color, tree.node(6).description) == (None, "Done")
    assert tree.node(2).types == "books.comic"  # narrowed to Genre's


def test_a_file_that_isnt_yaml_imports_nothing(env: Env) -> None:
    plan = env.plan("Genre: [Fantasy\n")
    assert plan.unreadable
    assert not plan.changes
    before = env.state()
    with pytest.raises(TagError, match="Line 2"):
        env.tags.import_tags("Genre: [Fantasy\n", "tags.yaml")
    assert env.state() == before
    assert env.tags.undo_label is None


def test_narrowing_applies_to_counts_the_uses_it_removes(env: Env) -> None:
    text = "Genre:\n  applies_to: [Comic]\n"
    assert env.plan(text).removed_uses == 1  # book 1's Fantasy
    env.tags.import_tags(text, "tags.yaml")
    assert (1, 2) not in env.state()[2]


def test_delete_option_deletes_only_unnamed_unused_tags(env: Env) -> None:
    # Fantasy (used) and Read (used) aren't named; Calm and Places > Ísland aren't either.
    text = "Genre: [Science Fiction]\nMood:\n"
    plan = env.plan(text, delete_unused=True)
    assert plan.deleted == (p("Mood", "Calm"), "Places", p("Places", "Ísland"))
    assert plan.kept == (p("Genre", "Fantasy"), "Read")
    assert env.plan(text).deleted == ()  # off by default
    env.tags.import_tags(text, "tags.yaml", delete_unused=True)
    tree = env.tree()
    assert {tree.node(t).name for t in tree} == {
        "Genre",
        "Fantasy",
        "Science Fiction",
        "Mood",
        "Read",
    }


def test_a_parent_is_kept_for_its_used_sub_tag(env: Env) -> None:
    plan = env.plan("Mood:\nPlaces:\n", delete_unused=True)
    assert "Genre" not in plan.deleted  # its sub-tag Fantasy is used
    assert p("Genre", "Science Fiction") in plan.deleted
    assert plan.kept == (p("Genre", "Fantasy"), "Read")


def test_an_alias_names_a_tag_for_the_delete_option(env: Env) -> None:
    plan = env.plan("Genre: [Sci-Fi]\n", delete_unused=True)
    assert p("Genre", "Science Fiction") not in plan.deleted


def test_one_undoable_step(env: Env) -> None:
    before = env.state()
    text = "Genre:\n  applies_to: [Comic]\n  tags:\n    Horror:\nMood: [Tense]\n"
    env.tags.import_tags(text, "tags.yaml", delete_unused=True)
    after = env.state()
    assert after != before
    label = env.tags.undo_label
    assert label == "Import tags from 'tags.yaml'"
    env.tags.undo()
    assert env.state() == before  # the tags, and book 1's Fantasy, back
    assert env.tags.undo_label is None
    env.tags.redo()
    assert env.state() == after
