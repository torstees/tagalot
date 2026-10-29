"""Promoting an Extra field to a field of the keep's own, and turning one back (#582)."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, select

from tagalot.builtin_themes.books import Book, Comic
from tagalot.core.extra_keys import EXTRA
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import ThemeRef, create_keep, load_keep_config
from tagalot.core.models import Entity, EntityField, FieldProvenance, FieldSource
from tagalot.core.own_fields import OwnField, OwnFieldError
from tagalot.core.promote import KeyValue, fits, parse_as, preview, type_counts
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings


@pytest.mark.parametrize(
    ("kind", "text", "value"),
    [
        ("text", "  Kobo ", "Kobo"),
        ("integer", "212", 212),
        ("integer", "-3", -3),
        ("integer", "1,995", 1995),
        ("integer", "1,99", None),
        ("integer", "2.5", None),
        ("number", "2.5", 2.5),
        ("number", "1,234.5", 1234.5),
        ("number", "nan", None),
        ("number", "lots", None),
        ("date", "2024-05-01", date(2024, 5, 1)),
        ("date", "2024-05-01 09:30", None),
        ("date", "May 1", None),
        ("yes/no", "Yes", True),
        ("yes/no", "false", False),
        ("yes/no", "0", False),
        ("yes/no", "maybe", None),
        ("link", "https://example.org/a", "https://example.org/a"),
        ("link", "example.org", None),
        ("text", "   ", None),
    ],
)
def test_parse_as(kind: str, text: str, value: Any) -> None:
    if value is None:
        assert not fits(kind, text)
    else:
        assert parse_as(kind, text) == value


def test_the_preview_counts_what_fits() -> None:
    values = [
        KeyValue(1, "books.book", "A", "pages", "212"),
        KeyValue(2, "books.book", "B", "pages", "about 300"),
        KeyValue(3, "books.comic", "C", "Pages", "32"),
    ]
    assert type_counts(values) == {"books.book": 2, "books.comic": 1}
    found = preview(values, "integer", ["books.book"])
    assert (found.items, found.fit) == (2, 1)
    assert [v.title for v in found.misfits] == ["B"]
    both = preview(values, "integer", ["books.book", "books.comic"])
    assert (both.items, both.fit) == (3, 2)
    assert preview(values, "text", ["books.book"]).misfits == ()


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    create_keep(tmp_path / "k", "Books", ThemeRef("books", 9))
    with KeepSession.open(tmp_path / "k", Settings()) as opened:

        def fill(conn: Connection) -> None:
            ctx = IngestSession(conn, opened.schema)
            ctx.upsert(Book, "dune", title="Dune")
            ctx.upsert(Book, "emma", title="Emma")
            ctx.upsert(Book, "heat", title="Heat")
            ctx.upsert(Comic, "maus", title="Maus")
            ctx.flush()

        opened.writer.run(fill)
        yield opened


def _id(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None
    return found


def _extra(session: KeepSession, title: str) -> dict[str, Any]:
    with session.reader.connect() as conn:
        return dict(conn.scalar(select(Entity.extra).where(Entity.id == _id(session, title))) or {})


def _sources(session: KeepSession, title: str) -> dict[str, FieldSource]:
    with session.reader.connect() as conn:
        rows = conn.execute(
            select(FieldProvenance.field, FieldProvenance.source).where(
                FieldProvenance.entity_id == _id(session, title)
            )
        )
        return {f: s for f, s in rows if f != "title"}


def _values(session: KeepSession) -> dict[tuple[str, str], Any]:
    with session.reader.connect() as conn:
        rows = conn.execute(
            select(Entity.title, EntityField.field, EntityField.integer, EntityField.text).join(
                Entity, Entity.id == EntityField.entity_id
            )
        )
        return {(t, f): i if i is not None else x for t, f, i, x in rows}


def _from_note(session: KeepSession, title: str, key: str) -> None:
    """Mark an Extra field as given by the item's note."""
    entity_id = _id(session, title)

    def mark(conn: Connection) -> None:
        conn.execute(
            FieldProvenance.__table__.insert().values(  # type: ignore[attr-defined]
                entity_id=entity_id, field=EXTRA + key, source=FieldSource.NOTE
            )
        )

    session.writer.run(mark)


@pytest.fixture
def pages(session: KeepSession) -> OwnField:
    """Three books and a comic with ``pages``: one from a note, one that isn't a number."""
    for title, key, value in [
        ("Dune", "pages", "412"),
        ("Emma", "Pages", "1,024"),
        ("Heat", "pages", "about 300"),
        ("Maus", "pages", "296"),
    ]:
        session.tags.edit_extra(_id(session, title), key, value, new=True)
    _from_note(session, "Dune", "pages")
    session.tags.clear_history()
    return OwnField("books.book", "pages", "Pages", "integer")


def test_promoting_moves_what_fits_with_its_provenance(
    session: KeepSession, pages: OwnField
) -> None:
    values = session.extra_key_values("PAGES")
    assert [v.title for v in values] == ["Dune", "Emma", "Heat", "Maus"]

    change = session.promote_extra("pages", [pages])
    assert change.label == "Make 'pages' the field 'Pages' (2 items)"
    assert change.moved == 2
    assert _values(session) == {("Dune", "pages"): 412, ("Emma", "pages"): 1024}
    assert _extra(session, "Dune") == {}
    assert _extra(session, "Heat") == {"pages": "about 300"}  # doesn't fit: stays
    assert _extra(session, "Maus") == {"pages": "296"}  # a comic: not chosen
    assert _sources(session, "Dune") == {"own:pages": FieldSource.NOTE}  # the note's
    assert _sources(session, "Emma") == {"own:pages": FieldSource.USER}  # typed by the user
    assert list(load_keep_config(session.keep.toml_path).fields["books.book"]) == ["pages"]


def test_one_undo_step_reverses_a_promotion(session: KeepSession, pages: OwnField) -> None:
    session.promote_extra("pages", [pages])
    assert session.tags.undo_label == "Make 'pages' the field 'Pages' (2 items)"
    session.tags.undo()
    assert _values(session) == {}
    assert _extra(session, "Emma") == {"Pages": "1,024"}
    assert _sources(session, "Dune") == {"extra:pages": FieldSource.NOTE}
    assert session.own_fields == {}
    assert load_keep_config(session.keep.toml_path).fields == {}
    session.tags.redo()
    assert _values(session) == {("Dune", "pages"): 412, ("Emma", "pages"): 1024}
    assert "pages" in session.own_fields["books.book"]


def test_promoting_into_several_types(session: KeepSession, pages: OwnField) -> None:
    comic = replace(pages, type_id="books.comic")
    session.promote_extra("pages", [pages, comic])
    assert _values(session)[("Maus", "pages")] == 296
    assert set(session.own_fields) == {"books.book", "books.comic"}


def test_a_promotion_that_cant_be_made_changes_nothing(
    session: KeepSession, pages: OwnField
) -> None:
    with pytest.raises(OwnFieldError, match="theme's fields"):
        session.promote_extra("pages", [replace(pages, label="Year")])
    with pytest.raises(OwnFieldError, match="Choose a type"):
        session.promote_extra("pages", [])
    session.add_own_field(pages)
    with pytest.raises(OwnFieldError, match="already a field"):
        session.promote_extra("pages", [pages])
    assert _extra(session, "Dune") == {"pages": "412"}
    assert session.tags.undo_label is None


def test_turning_back_keeps_a_notes_provenance(session: KeepSession, pages: OwnField) -> None:
    session.promote_extra("pages", [pages])
    session.tags.clear_history()
    own = session.own_fields["books.book"]["pages"]
    change = session.remove_own_field(own)
    assert change.label == "Turn 'Pages' back into an Extra field (2 items)"
    assert _extra(session, "Dune") == {"Pages": "412"}
    assert _extra(session, "Emma") == {"Pages": "1024"}
    assert _sources(session, "Dune") == {"extra:Pages": FieldSource.NOTE}
    assert _sources(session, "Emma") == {}  # the user's own Extra field again
    session.tags.undo()
    assert _values(session) == {("Dune", "pages"): 412, ("Emma", "pages"): 1024}
    assert _sources(session, "Dune") == {"own:pages": FieldSource.NOTE}
