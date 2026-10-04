"""File keywords (#294, DESIGN.md §7 "File keywords"): files' own tags become Tagalot tags
only by matching tags the user defined, stay in step with the files, and respect what the
user removed by hand, through tag operations and undo."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, insert, select

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.keywords import keyword_index, keyword_key, keywords_of
from tagalot.core.models import Entity, EntityTag, FileTagRemoval, Resource, ResourceKind, Root, Tag
from tagalot.core.tag_service import TagService
from tagalot.core.tags import TagNode, TagTree, TagTreeCache
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import EntityRef, Theme, role


class Note(ThemeEntity):
    table_name, type_id = "notes_note", "notes.note"
    roles = [role("file", kinds={"any"}, many=True)]


class Person(ThemeEntity):
    table_name, type_id = "notes_person", "notes.person"


class NotesTheme(Theme):
    id, name = "notes", "Notes"
    entities = [Note, Person]


# --- matching ---


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("Fantasy", "fantasy"),
        ("  Sci-Fi  ", "sci-fi"),
        ("Genre/Fantasy", "genre/fantasy"),
        ("Genre > Fantasy", "genre/fantasy"),
        ("Genre " + chr(0x203A) + " Fantasy", "genre/fantasy"),
        ("Ísland", "island"),
        ("/", ""),
        ("Genre//Fantasy", "genre/fantasy"),
    ],
)
def test_keyword_keys(text: str, key: str) -> None:
    assert keyword_key(text) == key


def _tree() -> TagTree:
    return TagTree(
        [
            TagNode(1, None, "Genre", None, 0),
            TagNode(2, 1, "Fantasy", None, 0),
            TagNode(3, 1, "Science fiction", None, 1),
            TagNode(4, None, "Mood", None, 1),
            TagNode(5, 4, "Dark", None, 0),
            TagNode(6, 1, "Dark", None, 2),  # "Dark" twice: by name, matches neither
        ],
        {3: ["Sci-Fi", "SF"], 2: ["Science fiction"]},  # an alias equal to another's name
    )


@pytest.mark.parametrize(
    ("keyword", "tag"),
    [
        ("Fantasy", 2),  # a unique name
        ("genre/fantasy", 2),  # a full path
        ("Genre > Science fiction", 3),  # a path beats an alias
        ("sci-fi", 3),  # an alias
        ("science fiction", 2),  # the alias beats the other tag's name
        ("Dark", None),  # two tags are called Dark
        ("Mood/Dark", 5),  # but their paths differ
        ("Horror", None),
    ],
)
def test_keyword_index(keyword: str, tag: int | None) -> None:
    assert keyword_index(_tree()).get(keyword_key(keyword)) == tag


# --- in a keep ---

GENRE, FANTASY, SCIFI, FAVORITES = 1, 2, 3, 4


@dataclass
class Env:
    engine: Engine
    writer: DbWriter
    schema: ThemeSchema
    tags: TagService

    def report(
        self, entity: int, resource: int, keywords: Sequence[str], kind: str = "note"
    ) -> None:
        """A scan reading ``keywords`` from a file of the item."""

        def job(conn: Connection) -> None:
            ctx = IngestSession(conn, self.schema)
            ctx.keywords(EntityRef(entity, f"notes.{kind}"), resource, keywords)
            ctx.flush()

        self.writer.run(job)

    def file_tags(self) -> set[tuple[int, int]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(EntityTag.entity_id, EntityTag.tag_id).where(EntityTag.by_file.is_(True))
            )
            return {(e, t) for e, t in rows}

    def own_tags(self) -> set[tuple[int, int]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(EntityTag.entity_id, EntityTag.tag_id).where(EntityTag.by_file.is_(False))
            )
            return {(e, t) for e, t in rows}

    def removals(self) -> set[tuple[int, int]]:
        with self.engine.connect() as conn:
            return {
                (e, t)
                for e, t in conn.execute(select(FileTagRemoval.entity_id, FileTagRemoval.tag_id))
            }


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "N", ThemeRef("notes", 1)))
    schema = open_theme(engine, keep, NotesTheme).schema
    with engine.begin() as conn:
        conn.execute(insert(Root).values(id="r", name="Notes", online=True))
        conn.execute(
            insert(Resource),
            [
                {"id": n, "root_id": "r", "relpath": f"f{n}.md", "kind": ResourceKind.FILE}
                for n in (10, 11, 12, 13)
            ],
        )
        for entity_id, kind in ((1, "note"), (2, "note"), (3, "person")):
            conn.execute(
                insert(Entity).values(id=entity_id, type=f"notes.{kind}", title=f"E{entity_id}")
            )
            conn.execute(insert(schema.metadata.tables[f"notes_{kind}"]).values(id=entity_id))
        conn.execute(
            insert(Tag),
            [
                {"id": GENRE, "parent_id": None, "name": "Genre"},
                {"id": FANTASY, "parent_id": GENRE, "name": "Fantasy"},
                {"id": SCIFI, "parent_id": GENRE, "name": "Science fiction"},
                {"id": FAVORITES, "parent_id": None, "name": "Favorites"},
            ],
        )
    with DbWriter(create_keep_engine(keep.db_path)) as writer:
        yield Env(engine, writer, schema, TagService(writer, TagTreeCache(engine), schema))
    engine.dispose()


def test_matching_keywords_give_tags_and_unmatched_ones_dont(env: Env) -> None:
    env.report(1, 10, ["Fantasy", "fantasy", "Space opera", "Genre/Science fiction"])
    assert env.file_tags() == {(1, FANTASY), (1, SCIFI)}
    with env.engine.connect() as conn:
        # As the file wrote them (the first spelling of each), alphabetically.
        assert keywords_of(conn, [1]) == {1: ["Fantasy", "Genre/Science fiction", "Space opera"]}
    assert env.own_tags() == set()  # nothing the user did


def test_tags_follow_the_files(env: Env) -> None:
    env.report(1, 10, ["Fantasy"])
    env.report(1, 11, ["Fantasy", "Science fiction"])  # a second file of the same item
    assert env.file_tags() == {(1, FANTASY), (1, SCIFI)}
    env.report(1, 11, [])  # that file no longer says so
    assert env.file_tags() == {(1, FANTASY)}  # the first file still does
    env.report(1, 10, [])
    assert env.file_tags() == set()


def test_a_tag_also_added_by_hand_stays(env: Env) -> None:
    env.tags.apply([1], [FANTASY])
    env.report(1, 10, ["Fantasy"])
    assert env.own_tags() == {(1, FANTASY)}  # the user's row, not a file tag
    env.report(1, 10, [])
    assert env.own_tags() == {(1, FANTASY)}


def test_a_file_tag_removed_by_hand_stays_off(env: Env) -> None:
    env.report(1, 10, ["Fantasy"])
    assert env.tags.remove([1], [FANTASY]) == 1
    assert env.removals() == {(1, FANTASY)}
    env.report(1, 10, ["Fantasy", "Science fiction"])  # a later scan
    assert env.file_tags() == {(1, SCIFI)}

    env.tags.undo()  # the removal comes back as a file tag, and isn't remembered
    assert env.removals() == set()
    assert env.file_tags() == {(1, FANTASY), (1, SCIFI)}
    assert env.own_tags() == set()
    env.tags.redo()
    assert env.removals() == {(1, FANTASY)}
    assert env.file_tags() == {(1, SCIFI)}

    env.tags.apply([1], [FANTASY])  # added back by hand: the record goes
    assert env.removals() == set()
    assert env.own_tags() == {(1, FANTASY)}
    env.tags.undo()
    assert env.removals() == {(1, FANTASY)}
    assert env.own_tags() == set()


def test_mapping_a_keyword_by_alias_tags_every_item_and_undoes(env: Env) -> None:
    env.report(1, 10, ["Space opera"])
    env.report(2, 12, ["space opera"])
    assert env.file_tags() == set()
    env.tags.add_alias(SCIFI, "Space opera")
    assert env.file_tags() == {(1, SCIFI), (2, SCIFI)}
    env.tags.undo()
    assert env.file_tags() == set()
    env.tags.redo()
    assert env.file_tags() == {(1, SCIFI), (2, SCIFI)}


def test_renames_deletes_and_merges(env: Env) -> None:
    env.report(1, 10, ["Speculative"])
    env.tags.rename(SCIFI, "Speculative")  # its name now matches
    assert env.file_tags() == {(1, SCIFI)}
    env.tags.delete(SCIFI)
    assert env.file_tags() == set()
    env.tags.undo()  # back with its file tags
    assert env.file_tags() == {(1, SCIFI)}

    env.report(2, 12, ["Fantasy"])
    env.tags.remove([2], [FANTASY])
    env.tags.merge(FANTASY, SCIFI)  # Fantasy's name becomes an alias of Speculative
    assert env.file_tags() == {(1, SCIFI)}  # item 2's removal moved with the merge
    assert env.removals() == {(2, SCIFI)}
    env.tags.undo()
    assert env.removals() == {(2, FANTASY)}
    assert env.file_tags() == {(1, SCIFI)}


def test_types_decide_too(env: Env) -> None:
    env.tags.set_types(GENRE, ["notes.note"])
    env.report(1, 10, ["Fantasy"])
    env.report(3, 13, ["Fantasy"], kind="person")  # people can't have genres
    assert env.file_tags() == {(1, FANTASY)}
    env.tags.set_types(GENRE, None)
    assert env.file_tags() == {(1, FANTASY), (3, FANTASY)}


def test_unlinking_a_file_drops_what_it_said(env: Env) -> None:
    note = EntityRef(1, "notes.note")

    def scan(conn: Connection, keep_linked: bool) -> None:
        ctx = IngestSession(conn, env.schema)
        ctx.link(note, 10, "file")
        ctx.keywords(note, 10, ["Fantasy"])
        if not keep_linked:
            ctx.unlink(note, 10, "file")  # the file no longer belongs to the note
        ctx.flush()

    env.writer.run(lambda conn: scan(conn, keep_linked=True))
    assert env.file_tags() == {(1, FANTASY)}
    env.writer.run(lambda conn: scan(conn, keep_linked=False))
    assert env.file_tags() == set()
    with env.engine.connect() as conn:
        assert keywords_of(conn, [1]) == {}
