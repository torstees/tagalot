"""The keep's own fields in searches (#583): columns' values, sorting, filters by their
search, choices, the preview, and saved searches whose field was removed."""

from dataclasses import replace
from datetime import date
from typing import Any

import pytest
from sqlalchemy import Connection

from tagalot.builtin_themes.books import Book, Comic
from tagalot.core.detail import load_preview
from tagalot.core.ingest import IngestSession
from tagalot.core.own_fields import OwnField
from tagalot.core.search import SearchError, choice_counts, count_matches, run_search
from tagalot.core.search_fields import field_values, scope_fields, search_fields
from tagalot.core.search_spec import (
    ChoiceFilter,
    RangeFilter,
    SearchSpec,
    SortKey,
    TextFilter,
)
from tagalot.core.session import KeepSession
from tagalot.core.tags import TagTree
from tests.core.test_own_fields import _id, session

__all__ = ["session"]  # the fixture

BOOK, COMIC = "books.book", "books.comic"
PAGES = OwnField(BOOK, "pages", "Pages", "integer", column=True)
SHOP = OwnField(BOOK, "shop", "Shop", search="choice")
WHERE = OwnField(BOOK, "where", "Where")
FINISHED = OwnField(BOOK, "finished", "Finished", "date")
OWNED = OwnField(BOOK, "owned", "Owned", "yes/no")
PRICE = OwnField(BOOK, "price", "Price", "number", search="none")


@pytest.fixture
def filled(session: KeepSession) -> KeepSession:
    """Dune (1965) and Emma (1815) from the fixture, plus Heat, each with values."""

    def more(conn: Connection) -> None:
        ctx = IngestSession(conn, session.schema)
        ctx.upsert(Book, "heat", title="Heat", year=1995)
        ctx.upsert(Comic, "maus", title="Maus")
        ctx.flush()

    session.writer.run(more)
    for own in (PAGES, SHOP, WHERE, FINISHED, OWNED, PRICE):
        session.add_own_field(own)
    values: dict[str, dict[OwnField, Any]] = {
        "Dune": {PAGES: 412, SHOP: "Kobo", WHERE: "Blackwell's Oxford", OWNED: True},
        "Emma": {PAGES: 1024, SHOP: "Kobo", FINISHED: date(2026, 1, 5), PRICE: 9.5},
        "Heat": {PAGES: 30, SHOP: "Amazon", OWNED: False},
    }
    for title, by_field in values.items():
        for own, value in by_field.items():
            session.tags.edit_field(_id(session, title), own.key, value)
    return session


def _search(session: KeepSession, spec: SearchSpec) -> list[str]:
    fields = search_fields(session.schema, spec.types, session.own_fields)
    with session.reader.connect() as conn:
        return [h.title for h in run_search(conn, spec, TagTree([], {}), fields=fields)]


def test_scope_fields_has_them_where_every_type_does(filled: KeepSession) -> None:
    own = filled.own_fields
    names = [f.name for f in scope_fields(filled.schema, (BOOK,), own)]
    assert names[-6:] == [
        "own:pages",
        "own:shop",
        "own:where",
        "own:finished",
        "own:owned",
        "own:price",
    ]
    assert not [f for f in scope_fields(filled.schema, (BOOK, COMIC), own) if "own:" in f.name]
    filled.add_own_field(OwnField(COMIC, "pages", "Pages", "integer"))
    both = [f.name for f in scope_fields(filled.schema, (BOOK, COMIC), filled.own_fields)]
    assert "own:pages" in both
    filled.add_own_field(OwnField(COMIC, "shop", "Shop", "integer"))  # another kind
    both = [f.name for f in scope_fields(filled.schema, (BOOK, COMIC), filled.own_fields)]
    assert "own:shop" not in both
    infos = {f.name: f for f in scope_fields(filled.schema, (BOOK,), filled.own_fields)}
    assert [infos[f"own:{n}"].spec.search for n in ("pages", "shop", "where", "price")] == [
        "range",
        "choice",
        "text",
        None,
    ]


@pytest.mark.parametrize(
    ("sort", "titles"),
    [
        (SortKey("own:pages"), ["Heat", "Dune", "Emma"]),
        (SortKey("own:pages", descending=True), ["Emma", "Dune", "Heat"]),
        (SortKey("own:shop"), ["Heat", "Dune", "Emma"]),
    ],
)
def test_sorting_by_them(filled: KeepSession, sort: SortKey, titles: list[str]) -> None:
    spec = SearchSpec(types=(BOOK,), sort=(sort, SortKey("title")))
    assert _search(filled, spec) == titles


@pytest.mark.parametrize(
    ("filter_", "titles"),
    [
        (RangeFilter("own:pages", 100, None), ["Dune", "Emma"]),
        (RangeFilter("own:pages", None, 500), ["Dune", "Heat"]),
        (RangeFilter("own:finished", date(2026, 1, 1), date(2026, 12, 31)), ["Emma"]),
        (ChoiceFilter("own:shop", ("Kobo",)), ["Dune", "Emma"]),
        (ChoiceFilter("own:owned", (True,)), ["Dune"]),
        (ChoiceFilter("own:owned", (False,)), ["Heat"]),
        (TextFilter("own:where", "oxford"), ["Dune"]),
    ],
)
def test_filtering_by_them(filled: KeepSession, filter_: Any, titles: list[str]) -> None:
    spec = SearchSpec(types=(BOOK,), fields=(filter_,), sort=(SortKey("title"),))
    assert _search(filled, spec) == titles


def test_choices_and_counts(filled: KeepSession) -> None:
    spec = SearchSpec(types=(BOOK,))
    fields = search_fields(filled.schema, spec.types, filled.own_fields)
    with filled.reader.connect() as conn:
        tree = TagTree([], {})
        assert choice_counts(conn, spec, tree, "own:shop", fields) == [("Kobo", 2), ("Amazon", 1)]
        assert sorted(choice_counts(conn, spec, tree, "own:owned", fields), key=str) == [
            (False, 1),
            (True, 1),
        ]
        ranged = SearchSpec(types=(BOOK,), fields=(RangeFilter("own:pages", 400, None),))
        assert count_matches(conn, ranged, tree, fields) == 2


def test_field_values_reads_them_as_their_kind(filled: KeepSession) -> None:
    spec = SearchSpec(types=(BOOK,), sort=(SortKey("title"),))
    fields = search_fields(filled.schema, spec.types, filled.own_fields)
    names = ["year", "own:pages", "own:owned", "own:finished", "own:price"]
    with filled.reader.connect() as conn:
        hits = run_search(conn, spec, TagTree([], {}), fields=fields)
        values = field_values(conn, filled.schema, hits, names, filled.own_fields)
    by_title = {h.title: values[h.id] for h in hits}
    assert by_title["Dune"] == {"year": 1965, "own:pages": 412, "own:owned": True}
    assert by_title["Emma"]["own:finished"] == date(2026, 1, 5)
    assert by_title["Emma"]["own:price"] == 9.5
    assert by_title["Heat"]["own:owned"] is False


def test_the_preview_shows_card_fields(filled: KeepSession) -> None:
    filled.change_own_field(replace(PAGES, card=True))
    with filled.reader.connect() as conn:
        preview = load_preview(conn, filled.schema, _id(filled, "Dune"), filled.own_fields)
    assert preview is not None
    assert [(f.name, f.label, f.value) for f in preview.facts][-1:] == [("own:pages", "Pages", 412)]


def test_a_saved_search_survives_a_rename_and_says_when_its_field_is_gone(
    filled: KeepSession,
) -> None:
    spec = SearchSpec(types=(BOOK,), fields=(RangeFilter("own:pages", 100, None),))
    filled.change_own_field(replace(PAGES, label="Page count"))
    assert sorted(_search(filled, spec)) == ["Dune", "Emma"]  # by name: still works
    filled.remove_own_field(filled.own_fields[BOOK]["pages"])
    with pytest.raises(SearchError, match="your field 'pages', which was removed"):
        _search(filled, spec)
