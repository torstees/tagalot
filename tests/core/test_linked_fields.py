"""Editing fields that mirror links (#440): a book's series, collections, and authors move
its links by hand (scans keep them), a universe it gets through its series follows the
series, and each edit is one undo step."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from tagalot.builtin_themes.books import Book, BooksTheme, Comic, Series
from tagalot.core.actions import ActionChange, restore_action
from tagalot.core.fields import FieldEditError
from tagalot.core.linked_fields import edit_linked_field, names_in, read_only_reason
from tagalot.core.models import Entity, FieldProvenance, FieldSource, Resource
from tests.themes.test_books import T0, Env, env

__all__ = ["env"]  # fixtures


def _id(env: Env, title: str) -> int:
    with env.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None, title
    return found


def _edit(env: Env, kind: type, title: str, name: str, value: Any) -> ActionChange:
    info = next(f for f in env.schema.entities[kind].fields if f.name == name)
    entity_id = _id(env, title)
    return env.writer.run(
        lambda conn: edit_linked_field(conn, env.schema, BooksTheme(), entity_id, info, value)
    )


def _reread(env: Env, minutes: int) -> None:
    env.writer.run(lambda conn: conn.execute(update(Resource).values(ingested_at=None)))
    env.scan(T0 + timedelta(minutes=minutes))


def _source(env: Env, title: str, name: str) -> FieldSource | None:
    with env.reader.connect() as conn:
        return conn.scalar(
            select(FieldProvenance.source).where(
                FieldProvenance.entity_id == _id(env, title), FieldProvenance.field == name
            )
        )


def test_names() -> None:
    assert names_in("Cosmere, Favourites ,cosmere") == ["Cosmere", "Favourites"]
    assert names_in("Pratchett, Terry; Gaiman, Neil") == ["Pratchett, Terry", "Gaiman, Neil"]
    assert names_in("  ") == []
    assert names_in(None) == []


def test_moving_a_book_to_another_series_lasts(env: Env) -> None:
    env.scan()
    change = _edit(env, Book, "Equal Rites", "series", "Witches")
    assert change.label == "Set Series of 'Equal Rites' to 'Witches'"
    assert "Equal Rites" in env.contents()["Witches"]
    assert "Equal Rites" not in env.contents()["Discworld"]
    assert env.fields(Book, "Equal Rites")["series"] == "Witches"
    assert _source(env, "Equal Rites", "series") == FieldSource.USER
    with env.reader.connect() as conn:  # made as a scan would make it: one item for both
        key = conn.scalar(select(Entity.ingest_key).where(Entity.title == "Witches"))
    assert key == "series:witches"

    _reread(env, 1)  # the file still says Discworld: the user's move stays
    assert "Equal Rites" in env.contents()["Witches"]
    assert "Equal Rites" not in env.contents()["Discworld"]
    assert env.fields(Book, "Equal Rites")["series"] == "Witches"

    env.writer.run(lambda conn: restore_action(conn, env.schema, change, forward=False))
    assert "Equal Rites" in env.contents()["Discworld"]
    assert env.fields(Book, "Equal Rites")["series"] == "Discworld"
    env.writer.run(lambda conn: restore_action(conn, env.schema, change, forward=True))
    assert env.fields(Book, "Equal Rites")["series"] == "Witches"


def test_collections_and_authors_take_several(env: Env) -> None:
    env.scan()
    _edit(env, Book, "Mistborn", "collections", "Cosmere, Favourites")
    contents = env.contents()
    assert "Mistborn" in contents["Cosmere"]
    assert "Mistborn" in contents["Favourites"]
    assert "Mistborn" not in contents.get("The Mistborn Trilogy", [])
    assert env.fields(Book, "Mistborn")["collections"] == "Cosmere, Favourites"

    _edit(env, Book, "Mistborn", "authors", "Brandon Sanderson; Feder, Moshe")
    assert env.credits("writers", "Mistborn") == ["Brandon Sanderson", "Feder, Moshe"]
    _reread(env, 1)
    assert env.credits("writers", "Mistborn") == ["Brandon Sanderson", "Feder, Moshe"]
    _edit(env, Book, "Mistborn", "authors", "")
    assert env.credits("writers", "Mistborn") == []
    assert env.fields(Book, "Mistborn")["authors"] is None

    with pytest.raises(FieldEditError, match="takes one name"):
        _edit(env, Book, "Mistborn", "series", "A, B")


def test_a_universe_through_a_series_follows_it(env: Env) -> None:
    env.scan()
    assert env.fields(Comic, "No Normal")["universe"] == "Marvel"
    info = next(f for f in env.schema.entities[Comic].fields if f.name == "universe")
    comic = _id(env, "No Normal")
    with env.reader.connect() as conn:
        reason = read_only_reason(conn, env.schema, comic, info)
    assert reason == "It comes from its series, Ms. Marvel: change it there."
    with pytest.raises(FieldEditError, match="change it there"):
        _edit(env, Comic, "No Normal", "universe", "DC")

    _edit(env, Series, "Ms. Marvel", "universe", "Marvel Comics")  # the series moves...
    assert "Ms. Marvel" in env.contents()["Marvel Comics"]
    assert env.fields(Comic, "No Normal")["universe"] == "Marvel Comics"  # ...its comic follows
    assert _source(env, "No Normal", "universe") == FieldSource.USER

    _edit(env, Book, "The Colour of Magic", "series", "Ms. Marvel")  # into a series: follows
    assert env.fields(Book, "The Colour of Magic")["universe"] == "Marvel Comics"


def test_the_loader_checks_mirrors() -> None:
    from tagalot.themes.api import Entity as ThemeEntity
    from tagalot.themes.api import Theme, contains, field, related
    from tagalot.themes.loader import validate_theme

    class Shelf(ThemeEntity):
        pass

    class Person(ThemeEntity):
        pass

    class Tome(ThemeEntity):
        shelf: str | None = field("Shelf", mirrors=Shelf)
        shelves: str | None = field("Shelves", mirrors=Shelf, many=True)
        writer: str | None = field("Writer", mirrors="wrote")
        count: int | None = field("Count", mirrors=Shelf)
        nowhere: str | None = field("Nowhere", mirrors=Person)
        nothing: str | None = field("Nothing", mirrors="nope")
        plain: str | None = field("Plain", many=True)

    class Tomes(Theme):
        id, name, api_version = "tomes", "Tomes", 14
        entities = [Shelf, Person, Tome]
        containment = [contains(Shelf, Tome)]
        relationships = [related("wrote", Person, Tome)]

    assert validate_theme(Tomes) == [
        "Tome.count: a field that mirrors a link must be text (str)",
        "Tome.nowhere: mirrors Person, but the theme doesn't declare contains(Person, Tome)",
        "Tome.nothing: mirrors 'nope', which it isn't part of",
        "Tome.plain: many needs mirrors",
        "a theme with fields that mirror links must set api_version = 15 (or later)",
    ]
