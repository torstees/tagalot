"""Notes (#379, DESIGN.md §9 *Notes*): a folder's Markdown note gives its item a title,
keywords, fields, Extra fields, and a body, ranked below the files' own details and above
online details, never over the user's edits; a theme names other folders with ctx.note."""

import datetime
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, delete, insert, select, text, update

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.detail import load_detail
from tagalot.core.entity_state import restore_states, snapshot_entities
from tagalot.core.fields import edit_extra, restore_extra
from tagalot.core.handlers import resource_to_open
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import (
    Entity,
    EntityKeyword,
    EntityNote,
    FieldProvenance,
    FieldSource,
    NoteKeyRemoval,
    Resource,
)
from tagalot.core.notes import (
    NoteText,
    as_text,
    body_without_title,
    convert,
    map_note,
    note_rank,
    read_notes,
)
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.triage import unlinked_files
from tagalot.core.writer import DbWriter
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import (
    IngestContext,
    Theme,
    entity_fields,
    field,
    role,
)


class Studio(ThemeEntity):
    country: str | None = field("Country", search="text")
    founded: int | None = field("Year founded")
    roles = [role("folder", kinds={"dir"}, primary=True)]


class Clip(ThemeEntity):
    length: float | None = field("Length")
    roles = [role("video", kinds={"video"}, primary=True)]


class Studios(Theme):
    id, name, version = "studios", "Studios", 1
    extensions = frozenset({".png", ".mp4"})  # no Markdown, like assets and movies
    dirs = True
    entities = [Studio, Clip]
    seen: list[str] = []

    def ingest(self, batch: Any, ctx: IngestContext) -> None:
        for resource in batch:
            Studios.seen.append(resource.relpath)
            if resource.kind == "dir" and "/" not in resource.relpath:
                name, _, year = resource.relpath.partition(" (")
                item = ctx.upsert(
                    Studio,
                    resource.relpath,
                    title=name,
                    founded=int(year.rstrip(")")) if year else None,
                )
                ctx.link(item, resource, "folder")
            elif resource.ext == ".mp4":
                clip = ctx.upsert(Clip, resource.relpath, title=resource.relpath.rsplit("/")[-1])
                ctx.link(clip, resource, "video")
                folder = ctx.resource_at(resource, resource.relpath.rpartition("/")[0])
                if folder is not None:
                    ctx.note(clip, folder)


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path
    clock: int = 0

    def scan(self) -> ScanReport:
        self.clock += 1
        when = datetime.datetime(2026, 10, 10, tzinfo=datetime.UTC) + datetime.timedelta(
            minutes=self.clock
        )
        root = RootConfig("r", "Studios", str(self.files))
        return scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=Studios, schema=self.schema
        )

    def note(self, relpath: str, text: str) -> Path:
        path = self.files / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        stamp = 1_700_000_000 + self.clock * 10 + len(text)  # always a new time
        os.utime(path, (stamp, stamp))
        return path

    def item(self, key: str) -> dict[str, Any]:
        with self.reader.connect() as conn:
            row = conn.execute(
                select(Entity.id, Entity.type, Entity.title, Entity.extra).where(
                    Entity.ingest_key == key
                )
            ).one()
            entity_table = self.schema.by_type_id(row.type)
            values = dict(
                conn.execute(select(entity_table.table).where(entity_table.table.c.id == row.id))
                .one()
                ._mapping
            )
            note = conn.execute(
                select(EntityNote.body).where(EntityNote.entity_id == row.id)
            ).scalar_one_or_none()
            keywords = sorted(
                conn.scalars(select(EntityKeyword.keyword).where(EntityKeyword.entity_id == row.id))
            )
            sources = dict(
                conn.execute(
                    select(FieldProvenance.field, FieldProvenance.source).where(
                        FieldProvenance.entity_id == row.id
                    )
                ).all()
            )
        return {
            "id": row.id,
            "title": row.title,
            "extra": dict(row.extra or {}),
            **{k: v for k, v in values.items() if k != "id"},
            "body": note,
            "keywords": keywords,
            "sources": sources,
        }

    def found(self, words: str) -> list[str]:
        with self.reader.connect() as conn:
            return sorted(
                conn.scalars(
                    text(
                        "SELECT e.title FROM entity_fts JOIN entity e ON e.id = entity_fts.rowid "
                        "WHERE entity_fts MATCH :q"
                    ),
                    {"q": words},
                )
            )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    files = tmp_path / "files"
    (files / "Aurora (2001)").mkdir(parents=True)
    (files / "Aurora (2001)" / "sky.png").write_bytes(b"png")
    (files / "Brightwater").mkdir()
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "S", ThemeRef("studios", 1)))
    schema = open_theme(engine, keep, Studios).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    Studios.seen = []
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, files)
    reader.dispose()
    engine.dispose()


AURORA = """---
title: Aurora Studio
tags: [Fantasy, Landscapes]
Country: Iceland
year_founded: 1999
website: https://aurora.example
cssclasses: wide
tagalot-type: studio
aliases: [Aurora]
---
# Aurora Studio

Painted skies, *mostly* at dusk.
"""


def test_a_folder_note_describes_its_item(env: Env) -> None:
    env.note("Aurora (2001)/Aurora (2001).md", AURORA)
    report = env.scan()
    aurora = env.item("Aurora (2001)")
    assert aurora["title"] == "Aurora Studio"  # over the folder's name
    assert aurora["country"] == "Iceland"  # by the field's label
    assert aurora["founded"] == 2001  # the folder gave it: files win
    assert aurora["extra"] == {"website": "https://aurora.example"}  # not cssclasses, tagalot-
    assert aurora["keywords"] == ["Fantasy", "Landscapes"]
    assert aurora["body"] == "Painted skies, *mostly* at dusk."  # without the title heading
    assert aurora["sources"]["title"] == FieldSource.NOTE
    assert aurora["sources"]["country"] == FieldSource.NOTE
    assert aurora["sources"]["extra:website"] == FieldSource.NOTE
    assert env.found("dusk") == ["Aurora Studio"]  # its body is searchable
    assert env.found("Iceland") == ["Aurora Studio"]
    assert not report.ingest_errors
    assert "Aurora (2001)/Aurora (2001).md" not in Studios.seen  # never the theme's
    with env.reader.connect() as conn:
        assert not [f for f in unlinked_files(conn) if f.relpath.endswith(".md")]


def test_a_note_fills_what_no_file_gives_and_keeps_it(env: Env) -> None:
    env.note("Brightwater/index.md", "---\nyear founded: 1987\n---\n")
    env.scan()
    assert env.item("Brightwater")["founded"] == 1987  # the folder gave none
    (env.files / "Brightwater" / "new.png").write_bytes(b"png")
    os.utime(env.files / "Brightwater", (1_800_000_000, 1_800_000_000))
    env.scan()  # the folder is read again, and gives no year: the note's stays
    assert Studios.seen.count("Brightwater") == 2
    assert env.item("Brightwater")["founded"] == 1987
    assert env.item("Brightwater")["title"] == "Brightwater"  # an index note has no title


def test_which_file_is_the_note(env: Env) -> None:
    env.note("Brightwater/README.md", "---\ntitle: From the readme\n---\n")
    env.scan()
    assert env.item("Brightwater")["title"] == "From the readme"
    env.note("Brightwater/index.markdown", "---\ntitle: From the index\n---\n")
    env.scan()
    assert env.item("Brightwater")["title"] == "From the index"
    env.note("Brightwater/BRIGHTWATER.md", "---\ntitle: Its own\n---\n")
    env.scan()
    assert env.item("Brightwater")["title"] == "Its own"
    with env.reader.connect() as conn:  # the others are files no item uses
        unused = sorted(f.relpath for f in unlinked_files(conn) if f.relpath.endswith("down"))
        unused += sorted(f.relpath for f in unlinked_files(conn) if f.relpath.endswith(".md"))
    assert unused == ["Brightwater/index.markdown", "Brightwater/README.md"]


def test_a_changed_or_removed_note(env: Env) -> None:
    path = env.note("Aurora (2001)/Aurora (2001).md", AURORA)
    env.scan()
    env.note("Aurora (2001)/Aurora (2001).md", "---\nCountry: Norway\n---\nNorthern lights.\n")
    env.scan()
    aurora = env.item("Aurora (2001)")
    assert aurora["title"] == "Aurora"  # the note stopped naming it: the folder's name again
    assert aurora["country"] == "Norway"
    assert aurora["extra"] == {}
    assert aurora["keywords"] == []
    assert aurora["body"] == "Northern lights."
    path.unlink()
    env.scan()
    aurora = env.item("Aurora (2001)")
    assert (aurora["title"], aurora["country"], aurora["body"]) == ("Aurora", None, None)
    assert "country" not in aurora["sources"]
    assert env.found("lights") == []


def test_the_users_edits_win(env: Env) -> None:
    env.note("Aurora (2001)/Aurora (2001).md", AURORA)
    env.scan()
    aurora = env.item("Aurora (2001)")

    def theirs(conn: Any) -> None:
        conn.execute(update(Entity).where(Entity.id == aurora["id"]).values(title="Mine"))
        conn.execute(
            update(FieldProvenance)
            .where(FieldProvenance.entity_id == aurora["id"], FieldProvenance.field == "title")
            .values(source=FieldSource.USER)
        )
        conn.execute(
            update(FieldProvenance)
            .where(
                FieldProvenance.entity_id == aurora["id"],
                FieldProvenance.field == "extra:website",
            )
            .values(source=FieldSource.USER)  # the user edited the note's Extra field
        )
        conn.execute(insert(NoteKeyRemoval).values(entity_id=aurora["id"], key="mood"))

    env.writer.run(theirs)
    env.note(
        "Aurora (2001)/Aurora (2001).md",
        "---\ntitle: Another\nwebsite: https://new.example\nmood: calm\n---\n",
    )
    env.scan()
    aurora = env.item("Aurora (2001)")
    assert aurora["title"] == "Mine"
    assert aurora["extra"] == {"website": "https://aurora.example"}  # kept, and no mood


def test_online_details_give_way_to_a_note(env: Env) -> None:
    env.note("Brightwater/index.md", "---\ncountry: Wales\n---\n")
    env.scan()
    item = env.item("Brightwater")

    class Fetching(IngestSession):
        source = FieldSource.FETCHED

    def look_up(conn: Any) -> None:
        ctx = Fetching(conn, env.schema)
        from tagalot.themes.api import EntityRef

        ctx.update(EntityRef(item["id"], "studios.studio"), country="Scotland", founded=1990)
        ctx.flush()

    env.writer.run(look_up)
    item = env.item("Brightwater")
    assert (item["country"], item["founded"]) == ("Wales", 1990)  # the note's stays
    assert item["sources"]["founded"] == FieldSource.FETCHED
    env.note("Brightwater/index.md", "---\ncountry: Wales\nyear founded: 1985\n---\n")
    env.scan()
    assert env.item("Brightwater")["founded"] == 1985  # a note replaces a looked-up value


def test_a_note_a_theme_names(env: Env) -> None:
    (env.files / "Clips" / "Dawn").mkdir(parents=True)
    (env.files / "Clips" / "Dawn" / "dawn.mp4").write_bytes(b"mp4")
    env.note("Clips/Dawn/README.md", "---\ntitle: Ignored\nlength: 12.5\n---\nShot at dawn.\n")
    env.scan()
    clip = env.item("Clips/Dawn/dawn.mp4")
    assert clip["title"] == "dawn.mp4"  # not its own folder: the note doesn't rename it
    assert (clip["length"], clip["body"]) == (12.5, "Shot at dawn.")


def test_a_note_that_cant_be_read(env: Env) -> None:
    env.note("Brightwater/index.md", "---\ncountry: Wales\n---\n")
    env.scan()
    env.note("Brightwater/index.md", "---\ncountry: [unclosed\n---\n")
    report = env.scan()
    assert env.item("Brightwater")["country"] == "Wales"  # left as it was
    assert [w.relpath for w in report.ingest_warnings] == ["Brightwater/index.md"]
    env.note("Brightwater/index.md", "---\nyear founded: soon\n---\n")
    report = env.scan()
    assert "year founded: isn't a whole number" in report.ingest_warnings[0].message


def test_a_missing_folder_keeps_its_note(env: Env) -> None:
    """Offline isn't missing isn't deleted: a folder the scan can't find keeps its note."""
    env.note("Brightwater/index.md", "---\ncountry: Wales\n---\nBy the sea.\n")
    env.scan()

    def gone(conn: Any) -> None:  # as a scan that found the folder and its note missing
        conn.execute(update(Resource).values(status="missing"))

    env.writer.run(gone)
    changes, problems = read_notes(env.reader, env.schema, "r", str(env.files))
    assert (changes, problems) == ([], [])
    assert env.item("Brightwater")["body"] == "By the sea."


def test_snapshots_carry_the_note(env: Env) -> None:
    """Undoing a merge or an action brings an item's note and removed note keys back."""
    env.note("Brightwater/index.md", "---\nmood: calm\n---\nBy the sea.\n")
    env.scan()
    item = env.item("Brightwater")

    def change(conn: Any) -> Any:
        before = snapshot_entities(conn, env.schema, [item["id"]])
        conn.execute(delete(EntityNote))
        conn.execute(insert(NoteKeyRemoval).values(entity_id=item["id"], key="mood"))
        restore_states(conn, env.schema, before)
        return conn.scalars(select(NoteKeyRemoval.key)).all()

    assert env.writer.run(change) == []
    assert env.item("Brightwater")["body"] == "By the sea."
    assert env.found("sea") == ["Brightwater"]


# --- names and mapping ---


@pytest.mark.parametrize(
    ("relpath", "rank"),
    [
        ("Aurora/Aurora.md", 0),
        ("Aurora/aurora.MARKDOWN", 0),
        ("Aurora/index.md", 1),
        ("Aurora/README.md", 2),
        ("Aurora/Readme.markdown", 2),
        ("Aurora/notes.md", None),
        ("Aurora/Aurora.txt", None),
        ("README.md", 2),  # the root's own; no item's folder
        ("A/B/B.md", 0),
    ],
)
def test_note_names(relpath: str, rank: int | None) -> None:
    assert note_rank(relpath) == rank


def test_mapping_a_note() -> None:
    note = NoteText(
        {
            "Title": "  Aurora  ",
            "keywords": "dusk, dawn",
            "COUNTRY": ["Iceland", "Norway"],
            "Year-Founded": "1,999",
            "aliases": ["A"],
            "tagalot-type": "studio",
            "Position": 3,
            "mood board": {"nested": True},
            "visited": datetime.date(2024, 5, 1),
            "open": True,
            "empty": "",
            "rating": 4.5,
        },
        "Body",
    )
    found = map_note(note, entity_fields(Studio), ignored=["Rating"])
    assert found.title == "Aurora"
    assert found.keywords == ["dusk", "dawn"]
    assert found.fields == {"country": "Iceland, Norway", "founded": 1999}
    assert found.extra == {"visited": "2024-05-01", "open": "true"}
    assert found.problems == []


@pytest.mark.parametrize(
    ("value", "kind", "expected"),
    [
        ("12", int, 12),
        (12.0, int, 12),
        ("2.5", float, 2.5),
        ("yes", bool, True),
        ("2024-05-01", datetime.date, datetime.date(2024, 5, 1)),
        (datetime.datetime(2024, 5, 1, 9, 30), datetime.date, datetime.date(2024, 5, 1)),
        (["a", 1], str, "a, 1"),
    ],
)
def test_converting_values(value: Any, kind: type, expected: Any) -> None:
    assert convert(value, kind) == expected


@pytest.mark.parametrize(
    ("value", "kind"), [("soon", int), (True, int), ("maybe", bool), ("May", datetime.date)]
)
def test_values_that_dont_convert(value: Any, kind: type) -> None:
    with pytest.raises(ValueError, match="isn't"):
        convert(value, kind)


def test_dates_and_times_as_utc() -> None:
    when = convert("2024-05-01T09:30:00+02:00", datetime.datetime)
    assert when == datetime.datetime(2024, 5, 1, 7, 30, tzinfo=datetime.UTC)
    assert as_text(datetime.datetime(2024, 5, 1, 9, 30)) == "2024-05-01 09:30"
    assert as_text({"a": 1}) is None


def test_the_title_heading() -> None:
    assert body_without_title("# Aurora\n\nSkies.", "aurora") == "Skies."
    assert body_without_title("\n# Aurora #\nSkies.", "Aurora") == "Skies."
    assert body_without_title("# Other\nSkies.", "Aurora") == "# Other\nSkies."
    assert body_without_title("Skies.", None) == "Skies."


# --- on the item's page (#380) ---


def test_editing_a_notes_extra_field_makes_it_the_users(env: Env) -> None:
    env.note("Aurora (2001)/Aurora (2001).md", AURORA)
    env.scan()
    aurora = env.item("Aurora (2001)")["id"]
    change = env.writer.run(
        lambda conn: edit_extra(conn, env.schema, aurora, "website", "https://mine.example")
    )
    assert env.item("Aurora (2001)")["sources"]["extra:website"] == FieldSource.USER
    env.note("Aurora (2001)/Aurora (2001).md", AURORA.replace("aurora.example", "new.example"))
    env.scan()
    assert env.item("Aurora (2001)")["extra"] == {"website": "https://mine.example"}
    env.writer.run(lambda conn: restore_extra(conn, env.schema, change, forward=False))
    restored = env.item("Aurora (2001)")
    assert restored["sources"]["extra:website"] == FieldSource.NOTE  # the note's again
    env.note("Aurora (2001)/Aurora (2001).md", AURORA.replace("aurora.example", "third.example"))
    env.scan()
    assert env.item("Aurora (2001)")["extra"] == {"website": "https://third.example"}


def test_removing_a_notes_extra_field_is_remembered(env: Env) -> None:
    env.note("Aurora (2001)/Aurora (2001).md", AURORA)
    env.scan()
    aurora = env.item("Aurora (2001)")["id"]
    change = env.writer.run(lambda conn: edit_extra(conn, env.schema, aurora, "website", None))
    assert change.label == "Remove 'website' from 'Aurora Studio'"
    removed = env.item("Aurora (2001)")
    assert removed["extra"] == {}
    assert "extra:website" not in removed["sources"]
    env.note("Aurora (2001)/Aurora (2001).md", AURORA + "\nMore.\n")
    env.scan()
    assert env.item("Aurora (2001)")["extra"] == {}  # the note still has it: left off
    with env.reader.connect() as conn:
        assert conn.scalars(select(NoteKeyRemoval.key)).all() == ["website"]

    env.writer.run(lambda conn: restore_extra(conn, env.schema, change, forward=False))
    undone = env.item("Aurora (2001)")
    assert undone["extra"] == {"website": "https://aurora.example"}
    assert undone["sources"]["extra:website"] == FieldSource.NOTE
    with env.reader.connect() as conn:
        assert conn.scalars(select(NoteKeyRemoval.key)).all() == []
    env.writer.run(lambda conn: restore_extra(conn, env.schema, change, forward=True))
    with env.reader.connect() as conn:
        assert conn.scalars(select(NoteKeyRemoval.key)).all() == ["website"]


def test_the_users_own_extra_fields_are_untouched(env: Env) -> None:
    env.scan()
    brightwater = env.item("Brightwater")["id"]
    env.writer.run(lambda conn: edit_extra(conn, env.schema, brightwater, "mood", "calm", new=True))
    env.writer.run(lambda conn: edit_extra(conn, env.schema, brightwater, "mood", None))
    item = env.item("Brightwater")
    assert item["extra"] == {}
    assert not [s for s in item["sources"] if s.startswith("extra:")]
    with env.reader.connect() as conn:
        assert conn.scalars(select(NoteKeyRemoval.key)).all() == []


def test_the_page_shows_the_note(env: Env) -> None:
    env.note("Aurora (2001)/Aurora (2001).md", AURORA)
    env.scan()
    aurora = env.item("Aurora (2001)")["id"]
    env.writer.run(lambda conn: edit_extra(conn, env.schema, aurora, "mood", "calm", new=True))
    root = str(env.files)
    with env.reader.connect() as conn:
        detail = load_detail(conn, env.schema, aurora, lambda _root: root)
        assert detail is not None
        assert detail.note_body == "Painted skies, *mostly* at dusk."
        assert detail.note_file is not None
        assert detail.note_file.relpath == "Aurora (2001)/Aurora (2001).md"
        assert detail.note_file.path == str(env.files / "Aurora (2001)" / "Aurora (2001).md")
        assert detail.extra_sources == (("website", FieldSource.NOTE),)  # not mood: the user's
        # Open note finds the file, though no role links it.
        found = resource_to_open(conn, aurora, detail.note_file.resource_id, lambda _r: root)
        assert [f.relpath for f in found] == ["Aurora (2001)/Aurora (2001).md"]
        brightwater = load_detail(conn, env.schema, env.item("Brightwater")["id"], lambda _r: root)
    assert brightwater is not None
    assert (brightwater.note_body, brightwater.note_file) == ("", None)
