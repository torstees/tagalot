"""A file's own front matter as Extra fields (#410, ``ctx.extra``): the notes' rules, marked
as from the file, the file winning over the item's note, the user's edits and removals
kept, and the ignored-keys lists applying to files too."""

import datetime
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.fields import edit_extra, restore_extra
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import FieldSource
from tagalot.core.note_keys import ignore_note_key, ignored_keys, restore_note_key
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.writer import DbWriter
from tagalot.themes.api import IngestContext, read_front_matter
from tests.core.test_notes import Env, Studio, Studios

INFO = "Aurora (2001)/info.md"
"""The studio's own file: a theme reads its front matter."""
NOTE = "Aurora (2001)/Aurora (2001).md"
"""The studio's note."""


class InfoStudios(Studios):
    """Studios whose ``info.md`` gives Extra fields from its front matter."""

    id = "infostudios"
    extensions = frozenset({".png", ".mp4", ".md"})

    def ingest(self, batch: Any, ctx: IngestContext) -> None:
        super().ingest(batch, ctx)
        for resource in batch:
            if resource.relpath.endswith("/info.md"):
                folder = resource.relpath.rpartition("/")[0]
                studio = ctx.find(Studio, title=folder.partition(" (")[0])
                if studio:
                    ctx.extra(studio[0], read_front_matter(resource.path)["other"])


class InfoEnv(Env):
    def scan(self, ignored: frozenset[str] | None = None) -> ScanReport:
        self.clock += 1
        when = datetime.datetime(2026, 10, 12, tzinfo=datetime.UTC) + datetime.timedelta(
            minutes=self.clock
        )
        root = RootConfig("r", "Studios", str(self.files))
        return scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=InfoStudios,
            schema=self.schema, note_ignore_keys=ignored,
        )  # fmt: skip

    def extra(self) -> dict[str, str]:
        return self.item("Aurora (2001)")["extra"]  # type: ignore[no-any-return]

    def sources(self) -> dict[str, FieldSource]:
        found = self.item("Aurora (2001)")["sources"]
        return {k: v for k, v in found.items() if k.startswith("extra:")}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[InfoEnv]:
    files = tmp_path / "files"
    (files / "Aurora (2001)").mkdir(parents=True)
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "S", ThemeRef(InfoStudios.id, 1)))
    schema = open_theme(engine, keep, InfoStudios).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        environment = InfoEnv(writer, reader, schema, files)
        environment.note(INFO, "---\nstatus: draft\n---\n")
        environment.scan()
        yield environment
    reader.dispose()
    engine.dispose()


INFO_KEYS = """---
title: Aurora
tags: [Skies]
status: first draft
written: 2024-05-01
palette: [teal, amber]
budget: {low: 1, high: 2}
cssclasses: wide
tagalot-type: studio
aliases: [A]
empty:
---
"""


def test_a_files_other_keys_are_extra_fields(env: InfoEnv) -> None:
    env.note(INFO, INFO_KEYS)
    env.scan()
    assert env.extra() == {
        "status": "first draft",
        "written": "2024-05-01",
        "palette": "teal, amber",
    }
    assert set(env.sources().values()) == {FieldSource.EXTRACTED}
    assert env.found("amber") == ["Aurora"]


def test_scans_keep_them_up_to_date(env: InfoEnv) -> None:
    env.note(INFO, "---\nstatus: done\nmood: calm\n---\n")
    env.scan()
    assert env.extra() == {"status": "done", "mood": "calm"}
    env.note(INFO, "---\nmood: calm\n---\n")
    env.scan()
    assert env.extra() == {"mood": "calm"}
    assert list(env.sources()) == ["extra:mood"]
    env.note(INFO, "No front matter now.\n")
    env.scan()
    assert env.extra() == {}


def test_the_users_edits_and_removals_stay(env: InfoEnv) -> None:
    env.note(INFO, "---\nstatus: draft\nmood: calm\n---\n")
    env.scan()
    aurora = env.item("Aurora (2001)")["id"]
    env.writer.run(lambda conn: edit_extra(conn, env.schema, aurora, "status", "final"))
    removal = env.writer.run(lambda conn: edit_extra(conn, env.schema, aurora, "mood", None))
    env.writer.run(lambda conn: edit_extra(conn, env.schema, aurora, "own", "mine", new=True))
    env.note(INFO, "---\nstatus: second draft\nmood: stormy\nown: theirs\n---\n")
    env.scan()
    assert env.extra() == {"status": "final", "own": "mine"}
    assert env.sources()["extra:status"] == FieldSource.USER

    env.writer.run(lambda conn: restore_extra(conn, env.schema, removal, forward=False))
    env.note(INFO, "---\nstatus: second draft\nmood: sunny\n---\n")
    env.scan()  # no longer removed: the file's again
    assert env.extra()["mood"] == "sunny"


def test_the_file_wins_over_the_note(env: InfoEnv) -> None:
    env.note(NOTE, "---\nstatus: from the note\nweather: fog\n---\nNotes.\n")
    env.note(INFO, "---\nstatus: from the file\n---\n")
    env.scan()
    assert env.extra() == {"status": "from the file", "weather": "fog"}
    assert env.sources() == {
        "extra:status": FieldSource.EXTRACTED,
        "extra:weather": FieldSource.NOTE,
    }
    env.note(INFO, "---\nother: x\n---\n")
    env.scan()  # the file stopped giving it: the note's comes back in the same scan
    assert env.extra() == {"status": "from the note", "weather": "fog", "other": "x"}
    assert env.sources()["extra:status"] == FieldSource.NOTE


def test_ignoring_a_key_from_a_page_covers_files(env: InfoEnv) -> None:
    env.note(INFO, "---\nstatus: draft\n---\n")
    env.scan(ignored_keys())
    change = env.writer.run(lambda conn: ignore_note_key(conn, env.schema, "Status"))
    assert change.label == "Ignore 'Status' in every note (1 item)"
    assert env.extra() == {}
    env.writer.run(lambda conn: restore_note_key(conn, env.schema, change, forward=False))
    assert env.extra() == {"status": "draft"}
    assert env.sources() == {"extra:status": FieldSource.EXTRACTED}


def test_a_changed_list_reads_the_files_again(env: InfoEnv) -> None:
    env.note(INFO, "---\nstatus: draft\nmood: calm\n---\n")
    env.scan(ignored_keys())
    env.scan(ignored_keys(["mood"]))  # the file didn't change; the list did
    assert env.extra() == {"status": "draft"}
    env.scan(ignored_keys())
    assert env.extra() == {"status": "draft", "mood": "calm"}


def test_reading_the_front_matter_gives_the_other_keys(tmp_path: Path) -> None:
    path = tmp_path / "book.md"
    path.write_text(
        "---\nTitle: X\nAuthor: Y\nDate: 2020-01-01\nSynopsis: Z\nImage: c.jpg\n"
        "status: draft\nRating: 4\n---\n",
        encoding="utf-8",
    )
    assert read_front_matter(str(path))["other"] == {"status": "draft", "Rating": 4}
