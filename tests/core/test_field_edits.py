"""Editing fields by hand: provenance, extraction respecting it, and undo (#93)."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, select

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.fields import FieldEditError, user_fields
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.models import Entity
from tagalot.core.search import run_search
from tagalot.core.search_fields import search_fields
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tag_service import TagService
from tagalot.core.tags import TagTree, TagTreeCache
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import Theme, field


class Movie(ThemeEntity):
    year: int | None = field("Year")
    rating: float | None = field("Rating")
    released: date | None = field("Released")
    seen_at: datetime | None = field("Seen")
    seen: bool = field("Seen it")
    note: str | None = field("Note", search="text")
    code: str = field("Code")
    size: int | None = field("Size", editable=False)


class MoviesTheme(Theme):
    id, name = "films", "Films"
    entities = [Movie]


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    service: TagService
    heat: int

    def values(self) -> dict[str, Any]:
        table = self.schema.entities[Movie].table
        with self.reader.connect() as conn:
            row = conn.execute(select(table).where(table.c.id == self.heat)).one()
            title = conn.scalar(select(Entity.title).where(Entity.id == self.heat))
        return {**row._mapping, "title": title}

    def edited(self) -> set[str]:
        with self.reader.connect() as conn:
            return user_fields(conn, self.heat)

    def ingest(self, **values: Any) -> None:
        def work(conn: Connection) -> None:
            ctx = IngestSession(conn, self.schema)
            ctx.upsert(Movie, "heat", **values)
            ctx.flush()

        self.writer.run(work)

    def search(self, text: str) -> list[str]:
        with self.reader.connect() as conn:
            hits = run_search(
                conn, SearchSpec(text=text), TagTree([], {}), fields=search_fields(self.schema, ())
            )
        return [h.title for h in hits]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "F", ThemeRef("films", 1)))
    schema = open_theme(engine, keep, MoviesTheme).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:

        def fill(conn: Connection) -> int:
            ctx = IngestSession(conn, schema)
            heat = ctx.upsert(Movie, "heat", title="Heat", year=1994, seen=False, code="H1")
            ctx.flush()
            return heat.id

        heat = writer.run(fill)
        service = TagService(writer, TagTreeCache(reader), schema)
        yield Env(writer, reader, schema, service, heat)
    reader.dispose()


def test_an_edit_sets_the_value_and_marks_it_the_users(env: Env) -> None:
    change = env.service.edit_field(env.heat, "year", 1995)
    assert env.values()["year"] == 1995
    assert env.edited() == {"year"}
    assert change.label == "Set Year of 'Heat' to 1995"
    assert env.service.undo_label == change.label


def test_extraction_leaves_edited_fields_alone(env: Env) -> None:
    env.service.edit_field(env.heat, "year", 1995)
    env.service.edit_field(env.heat, "title", "Heat (1995)")
    env.ingest(title="HEAT", year=2000, code="H2")  # a rescan
    values = env.values()
    assert (values["title"], values["year"], values["code"]) == ("Heat (1995)", 1995, "H2")


@pytest.mark.parametrize(
    ("name", "value", "stored"),
    [
        ("rating", 7, 7.0),  # a whole number for a number field
        ("rating", 7.5, 7.5),
        ("released", date(1995, 12, 15), date(1995, 12, 15)),
        # A time without a zone is local time, stored in UTC.
        ("seen_at", datetime(2026, 1, 2, 3, 4), datetime(2026, 1, 2, 3, 4).astimezone(UTC)),
        ("seen", True, True),
        ("note", "  great  ", "great"),
        ("note", "", None),  # empty clears an optional field
        ("year", None, None),
    ],
)
def test_values_of_each_type(env: Env, name: str, value: Any, stored: Any) -> None:
    env.service.edit_field(env.heat, name, value)
    assert env.values()[name] == stored


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("code", "", "Code can't be empty"),
        ("title", "   ", "Title can't be empty"),
        ("year", "1995", "Year must be a whole number"),
        ("year", True, "Year must be a whole number"),
        ("seen", 1, "Seen it must be yes or no"),
        ("released", datetime(1995, 1, 1), "Released is a date, not a date and time"),
        ("size", 10, "Size can't be edited"),
        ("nope", 1, "Movie has no field 'nope'"),
    ],
)
def test_values_that_dont_fit(env: Env, name: str, value: Any, message: str) -> None:
    with pytest.raises(FieldEditError, match=message):
        env.service.edit_field(env.heat, name, value)
    assert env.edited() == set()
    assert env.service.undo_label is None


def test_undo_and_redo_restore_the_value_and_its_provenance(env: Env) -> None:
    env.ingest(year=1994)  # extracted provenance on year
    env.service.edit_field(env.heat, "year", 1995)
    env.service.edit_field(env.heat, "year", 1996)
    assert env.service.undo() == "Set Year of 'Heat' to 1996"
    assert env.values()["year"] == 1995
    assert env.service.undo() == "Set Year of 'Heat' to 1995"
    assert env.values()["year"] == 1994
    assert env.edited() == set()  # back to extracted: a rescan may update it again
    env.ingest(year=1993)
    assert env.values()["year"] == 1993
    assert env.service.redo() == "Set Year of 'Heat' to 1995"
    assert env.values()["year"] == 1995
    assert env.edited() == {"year"}


def test_an_edit_that_changes_nothing_isnt_a_step(env: Env) -> None:
    env.service.edit_field(env.heat, "year", 1995)
    env.service.edit_field(env.heat, "year", 1995)
    env.service.undo()
    assert env.service.undo_label is None


def test_search_sees_edited_titles_and_text_fields(env: Env) -> None:
    env.service.edit_field(env.heat, "title", "Heat Wave")
    env.service.edit_field(env.heat, "note", "a Mann classic")
    assert env.search("wave") == ["Heat Wave"]
    assert env.search("mann") == ["Heat Wave"]
    env.service.undo()
    assert env.search("mann") == []


def test_editing_a_deleted_entity(env: Env) -> None:
    with pytest.raises(FieldEditError, match="no longer exists"):
        env.service.edit_field(999_999, "year", 1)


# --- extra fields (#95) ---


def _extra(env: Env) -> dict[str, Any]:
    with env.reader.connect() as conn:
        return dict(conn.scalar(select(Entity.extra).where(Entity.id == env.heat)) or {})


def test_add_change_and_remove_extra_fields(env: Env) -> None:
    change = env.service.edit_extra(env.heat, " Director ", " Michael Mann ", new=True)
    assert change.label == "Add 'Director' to 'Heat'"
    assert _extra(env) == {"Director": "Michael Mann"}
    env.service.edit_extra(env.heat, "Director", "M. Mann")
    assert _extra(env) == {"Director": "M. Mann"}
    removed = env.service.edit_extra(env.heat, "Director", None)
    assert removed.label == "Remove 'Director' from 'Heat'"
    assert _extra(env) == {}


def test_extra_fields_are_searchable_and_undoable(env: Env) -> None:
    env.service.edit_extra(env.heat, "Director", "Michael Mann", new=True)
    assert env.search("mann") == ["Heat"]
    env.service.edit_extra(env.heat, "Director", "")  # empty removes it
    assert env.search("mann") == []
    assert env.service.undo() == "Remove 'Director' from 'Heat'"
    assert _extra(env) == {"Director": "Michael Mann"}
    assert env.search("mann") == ["Heat"]
    env.service.undo()
    assert _extra(env) == {}
    assert env.service.redo() == "Add 'Director' to 'Heat'"
    assert _extra(env) == {"Director": "Michael Mann"}


def test_extra_fields_survive_a_rescan(env: Env) -> None:
    env.service.edit_extra(env.heat, "Director", "Michael Mann", new=True)
    env.ingest(title="Heat", year=1995)
    assert _extra(env) == {"Director": "Michael Mann"}


@pytest.mark.parametrize(
    ("name", "value", "new", "message"),
    [
        ("  ", "x", True, "A field needs a name"),
        ("Writer", "", True, "Type a value for 'Writer'"),
        ("Director", "Someone", True, "already has a field named 'Director'"),
        ("Writer", None, False, "has no field named 'Writer'"),
    ],
)
def test_extra_edits_that_dont_fit(
    env: Env, name: str, value: str | None, new: bool, message: str
) -> None:
    env.service.edit_extra(env.heat, "Director", "Michael Mann", new=True)
    with pytest.raises(FieldEditError, match=message):
        env.service.edit_extra(env.heat, name, value, new=new)
    assert _extra(env) == {"Director": "Michael Mann"}
