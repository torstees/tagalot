"""Ratings for any type (#577): a rating kind of the user's own fields, out of five in half
stars, turned on per type where the theme has no rating of its own."""

from dataclasses import replace

import pytest
from sqlalchemy import Connection, select

from tagalot.builtin_themes.books import Collection
from tagalot.core.fields import FieldEditError
from tagalot.core.ingest import IngestSession
from tagalot.core.models import Entity
from tagalot.core.notes import NoteText, map_note
from tagalot.core.own_fields import OwnField, OwnFieldError, field_problem, rating_problem
from tagalot.core.promote import fits
from tagalot.core.search_fields import scope_fields
from tagalot.core.search_spec import RangeFilter, SearchSpec, SortKey
from tagalot.core.session import KeepSession
from tests.core.test_own_fields import _id, session
from tests.core.test_own_fields_search import _search

__all__ = ["session"]  # the fixture

BOOK, COLLECTION = "books.book", "books.collection"
RATING = OwnField(COLLECTION, "rating", "Rating", "rating")


def test_a_rating_is_out_of_five_in_half_stars() -> None:
    assert RATING.python_type is float
    assert RATING.shown_as == "stars/5"
    assert RATING.search == "range"
    assert field_problem(RATING) is None
    assert field_problem(replace(RATING, search="choice")) is not None
    assert field_problem(replace(RATING, display="bytes")) == "Only a number can show as 'bytes'"
    assert [rating_problem(v) for v in (0, 3.5, 5)] == [None, None, None]
    assert rating_problem(6) == "isn't a rating from 0 to 5"
    assert rating_problem(3.25) == "isn't in half stars"
    assert fits("rating", "4.5")
    assert not fits("rating", "8")


def test_rating_a_type_adds_a_field_shown_on_cards_and_in_lists(session: KeepSession) -> None:
    assert session.theme_rates(BOOK)  # books have the theme's rating (#419)
    assert session.theme_rates("books.author")
    assert not session.theme_rates(COLLECTION)
    with pytest.raises(OwnFieldError, match="rates these items already"):
        session.rate_type(BOOK)
    rating = session.rate_type(COLLECTION)

    assert (rating.name, rating.label, rating.kind, rating.card, rating.column) == (
        "rating",
        "Rating",
        "rating",
        True,
        True,
    )
    assert session.own_rating(COLLECTION) == rating
    assert session.rate_type(COLLECTION) == rating  # once
    with pytest.raises(OwnFieldError, match="rating of yours already"):
        session.add_own_field(OwnField(COLLECTION, "stars", "Stars", "rating"))
    with pytest.raises(OwnFieldError, match="rates these items already"):
        session.add_own_field(OwnField(BOOK, "stars", "Stars", "rating"))
    [info] = [
        f
        for f in scope_fields(session.schema, (COLLECTION,), session.own_fields)
        if f.name == "own:rating"
    ]
    assert (info.spec.rating, info.spec.display, info.spec.search) == (5, "stars/5", "range")


def test_ratings_edit_sort_and_filter(session: KeepSession) -> None:
    session.rate_type(COLLECTION)

    def authors(conn: Connection) -> None:
        ctx = IngestSession(conn, session.schema)
        for name in ("Ann", "Bea", "Cy"):
            ctx.upsert(Collection, name, title=name)
        ctx.flush()

    session.writer.run(authors)
    for name, stars in (("Ann", 4.5), ("Bea", 2), ("Cy", 5)):
        session.tags.edit_field(_id(session, name), RATING.key, stars)
    with pytest.raises(FieldEditError, match="isn't in half stars"):
        session.tags.edit_field(_id(session, "Ann"), RATING.key, 3.3)
    with pytest.raises(FieldEditError, match="from 0 to 5"):
        session.tags.edit_field(_id(session, "Ann"), RATING.key, 7)
    spec = SearchSpec(types=(COLLECTION,), sort=(SortKey(RATING.key, descending=True),))
    assert _search(session, spec) == ["Cy", "Ann", "Bea"]
    best = SearchSpec(types=(COLLECTION,), fields=(RangeFilter(RATING.key, 4, None),))
    assert sorted(_search(session, best)) == ["Ann", "Cy"]


def test_front_matter_ratings_must_fit() -> None:
    found = map_note(NoteText({"Rating": 4.5}, ""), [], own=[RATING])
    assert found.own == {"rating": 4.5}
    bad = map_note(NoteText({"rating": 8}, ""), [], own=[RATING])
    assert bad.own == {}
    assert bad.extra == {"rating": "8"}
    assert bad.problems == ["rating: isn't a rating from 0 to 5, so it stays an Extra field"]


def test_turning_ratings_off_keeps_them_as_extra_fields(session: KeepSession) -> None:
    rating = session.rate_type(COLLECTION)

    def collection(conn: Connection) -> None:
        ctx = IngestSession(conn, session.schema)
        ctx.upsert(Collection, "ann", title="Ann")
        ctx.flush()

    session.writer.run(collection)
    session.tags.edit_field(_id(session, "Ann"), rating.key, 3.5)
    session.remove_own_field(rating)
    assert session.own_rating(COLLECTION) is None
    with session.reader.connect() as conn:
        assert conn.scalar(select(Entity.extra).where(Entity.title == "Ann")) == {"Rating": "3.5"}
    session.tags.undo()
    assert session.own_rating(COLLECTION) == rating
