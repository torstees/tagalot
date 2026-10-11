"""The keep's notes folder (#382, DESIGN.md §9 *The notes folder*): only its Markdown files
are recorded and none reach the theme; each note is typed by ``tagalot-type`` or its
folder, matched to an item by title or aliases (case and accents aside), and applied as a
note; a folder note wins; offline notes stay; Windows and UNC paths find the folder."""

import datetime
import ntpath
import os
import posixpath
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import (
    RootConfig,
    ThemeRef,
    clean_relpath,
    create_keep,
    load_keep_config,
    save_keep_config,
)
from tagalot.core.models import Entity, EntityNote, NotesFolderNote, Resource, ResourceStatus
from tagalot.core.notes_folder import note_type, type_names
from tagalot.core.root_admin import (
    folder_in_root,
    notes_folder_for,
    with_notes_folder,
    without_root,
)
from tagalot.core.scanjob import ScanReport, scan_notes_folder, scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import IngestContext, Theme, field, role
from tests.core.test_keep_toml import _config


class Studio(ThemeEntity):
    roles = [role("folder", kinds={"dir"}, primary=True)]


class Clip(ThemeEntity):
    roles = [role("video", kinds={"video"}, primary=True)]


class Actor(ThemeEntity):
    born: int | None = field("Born")


class Cast(Theme):
    """Clips named ``Title [Actor, Actor].mp4`` in studio folders; actors have no files."""

    id, name, version = "cast", "Cast", 1
    extensions = frozenset({".mp4", ".png"})
    dirs = True
    entities = [Studio, Clip, Actor]
    seen: list[str] = []

    def ingest(self, batch: Any, ctx: IngestContext) -> None:
        for resource in batch:
            Cast.seen.append(resource.relpath)
            if resource.kind == "dir" and "/" not in resource.relpath:
                studio = ctx.upsert(Studio, resource.relpath, title=resource.relpath)
                ctx.link(studio, resource, "folder")
            elif resource.ext == ".mp4":
                name = posixpath.basename(resource.relpath)[:-4]
                title, _, cast = name.partition(" [")
                clip = ctx.upsert(Clip, resource.relpath, title=title)
                ctx.link(clip, resource, "video")
                for actor in cast.rstrip("]").split(","):
                    if actor.strip():
                        ctx.upsert(Actor, actor.strip().casefold(), title=actor.strip())


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path
    notes: str = "Notes"
    clock: int = 0

    @property
    def root(self) -> RootConfig:
        return RootConfig("r", "Films", str(self.files))

    def scan(self, path: str | None = None) -> ScanReport:
        self.clock += 1
        when = datetime.datetime(2026, 10, 11, tzinfo=datetime.UTC) + datetime.timedelta(
            minutes=self.clock
        )
        root = self.root
        report = scan_root(
            self.writer,
            self.reader,
            root,
            path or root.path,
            when=when,
            theme=Cast,
            schema=self.schema,
            notes_folder=self.notes,
        )
        notes = scan_notes_folder(
            self.writer,
            self.reader,
            [root],
            lambda _: path or root.path,
            root.id,
            self.notes,
            theme=Cast,
            schema=self.schema,
        )
        report.ingest_warnings += notes.ingest_warnings
        return report

    def write(self, relpath: str, text: str) -> Path:
        path = self.files / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        stamp = 1_700_000_000 + self.clock * 10 + len(text)
        os.utime(path, (stamp, stamp))
        return path

    def item(self, title: str) -> dict[str, Any]:
        with self.reader.connect() as conn:
            row = conn.execute(
                select(Entity.id, Entity.type, Entity.extra).where(Entity.title == title)
            ).one()
            note = conn.execute(
                select(EntityNote.body, Resource.relpath)
                .join(Resource, Resource.id == EntityNote.resource_id, isouter=True)
                .where(EntityNote.entity_id == row.id)
            ).one_or_none()
            table = self.schema.by_type_id(row.type)
            values = conn.execute(select(table.table).where(table.table.c.id == row.id)).one()
        return {
            "id": row.id,
            "extra": dict(row.extra or {}),
            "body": note.body if note else None,
            "note": note.relpath if note else None,
            **{k: v for k, v in values._mapping.items() if k != "id"},
        }

    def rows(self) -> dict[str, tuple[int, bool, str | None]]:
        """Note path -> (matches, applied, the matched item's title)."""
        with self.reader.connect() as conn:
            rows = conn.execute(
                select(
                    Resource.relpath,
                    NotesFolderNote.matches,
                    NotesFolderNote.applied,
                    Entity.title,
                )
                .join(Resource, Resource.id == NotesFolderNote.resource_id)
                .join(Entity, Entity.id == NotesFolderNote.item_id, isouter=True)
            ).all()
        return {r.relpath: (r.matches, r.applied, r.title) for r in rows}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    files = tmp_path / "files"
    (files / "Aurora").mkdir(parents=True)
    (files / "Aurora" / "Heat [Al Pacino, Robert De Niro].mp4").write_bytes(b"mp4")
    (files / "Aurora" / "Léon [Jean Reno].mp4").write_bytes(b"mp4")
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "C", ThemeRef("cast", 1)))
    schema = open_theme(engine, keep, Cast).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    Cast.seen = []
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, files)
    reader.dispose()
    engine.dispose()


PACINO = """---
title: Al Pacino
born: 1940
award: Oscar
---
Born in East Harlem.
"""


def test_the_notes_folder_is_read_only_for_notes(env: Env) -> None:
    env.write("Notes/Actors/Al Pacino.md", PACINO)
    (env.files / "Notes" / "Actors" / "pacino.png").write_bytes(b"png")
    (env.files / "Notes" / "Actors" / "Old.mp4").write_bytes(b"mp4")
    env.scan()
    assert not [p for p in Cast.seen if p.startswith("Notes")]  # nothing reaches the theme
    with env.reader.connect() as conn:
        recorded = set(conn.scalars(select(Resource.relpath)))
    assert {p for p in recorded if p.startswith("Notes")} == {"Notes/Actors/Al Pacino.md"}
    pacino = env.item("Al Pacino")
    assert pacino["born"] == 1940
    assert pacino["extra"] == {"award": "Oscar"}
    assert pacino["body"] == "Born in East Harlem."
    assert pacino["note"] == "Notes/Actors/Al Pacino.md"


def test_typing_a_note(env: Env) -> None:
    names = type_names(env.schema)
    actor = env.schema.by_type_id("cast.actor").type_id
    assert note_type(names, "actors", "Clips") == (actor, None)  # the key wins
    assert note_type(names, None, "Notes/ACTORS") == (actor, None)
    assert note_type(names, None, "") == (None, None)  # any type
    found, problem = note_type(names, "Directors", "Actors")
    assert found == actor
    assert problem == "tagalot-type: 'Directors' is no type of this keep"


def test_matching_by_title_file_name_and_aliases(env: Env) -> None:
    env.write("Notes/Robert De Niro.md", "---\nborn: 1943\n---\n")  # its file name
    env.write("Notes/jean.md", "---\ntitle: Jean\naliases: [JEAN RENO]\n---\n")
    env.write("Notes/Leon.md", "---\ntagalot-type: clip\n---\nThe professional.\n")  # accents
    env.scan()
    assert env.item("Robert De Niro")["born"] == 1943
    assert env.item("Jean Reno")["note"] == "Notes/jean.md"
    assert env.item("Léon")["body"] == "The professional."
    assert env.rows()["Notes/Leon.md"] == (1, True, "Léon")


def test_notes_that_dont_find_one_item(env: Env) -> None:
    env.write("Notes/Nobody.md", "Who?\n")
    env.write("Notes/Aurora.md", "---\naliases: [Heat]\n---\n")  # a studio and a clip
    env.scan()
    assert env.rows() == {
        "Notes/Nobody.md": (0, False, None),
        "Notes/Aurora.md": (2, False, None),
    }
    env.write("Notes/Aurora.md", "---\naliases: [Heat]\ntagalot-type: studio\n---\nA studio.\n")
    env.scan()
    assert env.rows()["Notes/Aurora.md"] == (1, True, "Aurora")
    assert env.item("Aurora")["body"] == "A studio."


def test_a_folder_note_wins_and_two_notes_for_one_item(env: Env) -> None:
    env.write("Aurora/Aurora.md", "From the folder.\n")
    env.write("Notes/Aurora.md", "---\ntagalot-type: studio\n---\nFrom the notes folder.\n")
    env.write("Notes/Actors/Al Pacino.md", PACINO)
    env.write("Notes/Pacino.md", "---\naliases: [al pacino]\n---\nAnother.\n")
    env.scan()
    assert env.item("Aurora")["body"] == "From the folder."
    assert env.rows()["Notes/Aurora.md"] == (1, False, "Aurora")  # Triage: a second note
    assert env.item("Al Pacino")["note"] == "Notes/Actors/Al Pacino.md"  # first by path
    assert env.rows()["Notes/Pacino.md"] == (1, False, "Al Pacino")

    (env.files / "Aurora" / "Aurora.md").unlink()  # the folder note goes: the other applies
    env.scan()
    assert env.item("Aurora")["body"] == "From the notes folder."
    env.scan()  # and stays
    assert env.item("Aurora")["body"] == "From the notes folder."


def test_a_new_item_finds_its_note(env: Env) -> None:
    env.write("Notes/Actors/Natalie Portman.md", "---\nborn: 1981\n---\n")
    env.scan()
    assert env.rows()["Notes/Actors/Natalie Portman.md"] == (0, False, None)
    (env.files / "Aurora" / "Léon [Jean Reno, Natalie Portman].mp4").write_bytes(b"mp4")
    env.scan()
    assert env.item("Natalie Portman")["born"] == 1981


def test_a_changed_and_a_removed_note(env: Env) -> None:
    path = env.write("Notes/Actors/Al Pacino.md", PACINO)
    env.scan()
    env.write("Notes/Actors/Al Pacino.md", PACINO.replace("Oscar", "Tony"))
    env.scan()
    assert env.item("Al Pacino")["extra"] == {"award": "Tony"}
    path.unlink()
    env.scan()
    pacino = env.item("Al Pacino")
    assert (pacino["born"], pacino["extra"], pacino["note"]) == (None, {}, None)
    assert env.rows() == {}


def test_an_offline_notes_folder_keeps_its_notes(env: Env, tmp_path: Path) -> None:
    env.write("Notes/Actors/Al Pacino.md", PACINO)
    env.scan()
    report = env.scan(str(tmp_path / "unplugged"))
    assert not report.online
    pacino = env.item("Al Pacino")
    assert pacino["born"] == 1940
    assert pacino["note"] == "Notes/Actors/Al Pacino.md"
    with env.reader.connect() as conn:
        assert set(conn.scalars(select(Resource.status))) == {ResourceStatus.OFFLINE}
    assert env.rows()["Notes/Actors/Al Pacino.md"] == (1, True, "Al Pacino")


def test_a_whole_root_of_notes(env: Env) -> None:
    env.notes = ""
    env.write("Al Pacino.md", PACINO)
    env.scan()
    assert Cast.seen == []  # the whole root is notes: no studios, no clips
    with env.reader.connect() as conn:
        assert list(conn.scalars(select(Resource.relpath))) == ["Al Pacino.md"]


# --- choosing it ---


@pytest.mark.parametrize(
    ("folder", "root", "pathmod", "inside"),
    [
        (r"C:\Films\Notes\Actors", r"c:\films", ntpath, "Notes/Actors"),
        (r"C:\Films", "C:/Films/", ntpath, ""),
        (r"\\nas\films\Notes", r"\\NAS\Films", ntpath, "Notes"),
        (r"\\nas\films2\Notes", r"\\nas\films", ntpath, None),
        (r"D:\Notes", r"C:\Films", ntpath, None),
        ("/mnt/films/Notes", "/mnt/films", posixpath, "Notes"),
        ("/mnt/Films/Notes", "/mnt/films", posixpath, None),  # case counts here
    ],
)
def test_folder_in_root(folder: str, root: str, pathmod: Any, inside: str | None) -> None:
    assert folder_in_root(folder, root, pathmod) == inside


def test_keep_toml_and_the_notes_folder(tmp_path: Path) -> None:
    films, notes = RootConfig("films", "Films", r"\\nas\films"), RootConfig("n", "N", r"D:\N")
    config = with_notes_folder(_config(films, notes), "films", "Notes\\Actors\\")
    assert (config.notes_root, config.notes_path) == ("films", "Notes/Actors")
    assert notes_folder_for(
        config, lambda r: {"films": r"\\nas\films"}[r], r"\\NAS\films\x", ntpath
    ) == (
        "films",
        "x",
    )
    path = tmp_path / "keep.toml"
    save_keep_config(config, path)
    assert "[notes]\nroot = 'films'\npath = 'Notes/Actors'" in path.read_text(encoding="utf-8")
    loaded = load_keep_config(path)
    assert (loaded.notes_root, loaded.notes_path) == ("films", "Notes/Actors")
    assert without_root(config, "films").notes_root is None
    assert without_root(config, "n").notes_root == "films"
    assert with_notes_folder(config, None).notes_root is None
    assert clean_relpath("./Notes//Actors/") == "Notes/Actors"
