"""The keep's own fields (#581): keep.toml, checks, values, edits and undo, search, page,
removal, merges."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Connection, select

from tagalot.builtin_themes.books import Book
from tagalot.core.detail import load_detail
from tagalot.core.fields import FieldEditError
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import (
    ThemeRef,
    create_keep,
    dump_keep_config,
    load_keep_config,
    save_keep_config,
)
from tagalot.core.merge import MergeChange, merge_items, plan_merge, restore_merge
from tagalot.core.models import Entity, EntityField, FieldProvenance, FieldSource
from tagalot.core.own_fields import (
    OwnField,
    OwnFieldError,
    changed,
    clash,
    field_problem,
    name_for,
    parse_fields,
)
from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import TagTree

PAGES = OwnField("books.book", "pages_read", "Pages read", "integer", column=True)
FINISHED = OwnField("books.book", "finished", "Finished", "date")
WHERE = OwnField("books.book", "where_bought", "Where bought")
OWNED = OwnField("books.book", "owned", "Owned", "yes/no")


# --- keep.toml ---


def test_fields_round_trip_through_keep_toml(tmp_path: Path) -> None:
    keep = create_keep(tmp_path / "k", "Books", ThemeRef("books", 9))
    keep.config.fields = {
        "books.book": {
            "pages_read": OwnField(
                "books.book", "pages_read", "Pages read", "integer", "choice", "bytes", True, True
            ),
            "where_bought": WHERE,
        }
    }
    save_keep_config(keep.config, keep.toml_path)
    text = keep.toml_path.read_text(encoding="utf-8")
    assert "[fields.'books.book'.pages_read]" in text
    assert "search = 'choice'" in text  # not the kind's default, so written
    assert "search" not in text.split("where_bought]")[1]  # the default isn't
    again = load_keep_config(keep.toml_path)
    assert again.fields == keep.config.fields
    assert dump_keep_config(again) == text


def test_a_field_with_a_problem_is_left_out_and_the_keep_opens(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    keep = create_keep(tmp_path / "k", "Books", ThemeRef("books", 9))
    keep.toml_path.write_text(
        keep.toml_path.read_text(encoding="utf-8")
        + '\n[fields."books.book".good]\nlabel = "Good"\n'
        + '\n[fields."books.book".Bad-Name]\nlabel = "Bad"\n'
        + '\n[fields."books.book".odd]\nkind = "colour"\n'
        + '\n[fields."books.book".flag]\nkind = "yes/no"\ncard = "yes"\n',
        encoding="utf-8",
    )
    config = load_keep_config(keep.toml_path)
    assert list(config.fields["books.book"]) == ["good"]
    for problem in ("Bad-Name", "colour", "card"):
        assert problem in caplog.text


@pytest.mark.parametrize(
    ("own", "problem"),
    [
        (OwnField("t", "pages", "Pages", "integer"), None),
        (OwnField("t", "Pages", "Pages"), "lowercase"),
        (OwnField("t", "1st", "First"), "lowercase"),
        (OwnField("t", "x", " "), "needs a label"),
        (OwnField("t", "x", "X", "colour"), "isn't a kind"),
        (OwnField("t", "x", "X", "date", "words"), "can't be searched"),
        (OwnField("t", "x", "X", "text", display="bytes"), "Only a number"),
        (OwnField("t", "x", "X", "number", display="stars"), "isn't a display"),
        (OwnField("t", "x", "X", "number", display="duration"), None),
    ],
)
def test_field_problems(own: OwnField, problem: str | None) -> None:
    found = field_problem(own)
    if problem is None:
        assert found is None
    else:
        assert found is not None
        assert problem in found


def test_each_kind_has_its_default_search() -> None:
    kinds = ["text", "integer", "number", "date", "yes/no", "link"]
    searches = [OwnField("t", "x", "X", k).search for k in kinds]
    assert searches == ["words", "range", "range", "range", "choice", "words"]


def test_names_and_labels_may_not_clash() -> None:
    theme = [("year", "Year"), ("publisher", "Publisher")]
    assert clash(OwnField("t", "pages", "Pages"), theme, "Title") is None
    assert "theme's fields" in (clash(OwnField("t", "year", "When"), theme, "Title") or "")
    assert "key alias" in (clash(OwnField("t", "pub", "publisher"), theme, "Title") or "")
    assert "title" in (clash(OwnField("t", "name", "Title"), theme, "Title") or "")
    other = OwnField("t", "pages", "Pages")
    assert "already" in (clash(OwnField("t", "p", "PAGES"), theme, "Title", [other]) or "")
    assert clash(changed(other, label="Pages so far"), theme, "Title", [other]) is None


def test_names_made_from_labels() -> None:
    assert name_for("Pages read") == "pages_read"
    assert name_for("Pages read", ["pages_read"]) == "pages_read_2"
    assert name_for("2nd copy") == "field_2nd_copy"
    assert name_for("!!") == "field"


def test_a_fields_name_and_kind_stay() -> None:
    with pytest.raises(ValueError, match="stay"):
        changed(PAGES, kind="text")
    assert changed(PAGES, label="Read").name == "pages_read"


def test_parse_fields_needs_tables() -> None:
    found, problems = parse_fields({"t": {"x": "nope"}, "u": 3})
    assert found == {}
    assert len(problems) == 2


# --- a session ---


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    create_keep(tmp_path / "k", "Books", ThemeRef("books", 9))
    with KeepSession.open(tmp_path / "k", Settings()) as opened:

        def fill(conn: Connection) -> None:
            ctx = IngestSession(conn, opened.schema)
            ctx.upsert(Book, "dune", title="Dune", year=1965)
            ctx.upsert(Book, "emma", title="Emma", year=1815)
            ctx.flush()

        opened.writer.run(fill)
        yield opened


def _id(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None
    return found


def _rows(session: KeepSession) -> list[tuple[int, str, object, object, object, object]]:
    with session.reader.connect() as conn:
        rows = conn.execute(
            select(
                EntityField.entity_id,
                EntityField.field,
                EntityField.text,
                EntityField.integer,
                EntityField.real,
                EntityField.day,
            ).order_by(EntityField.entity_id, EntityField.field)
        )
        return [tuple(r) for r in rows]  # type: ignore[misc]


def _search(session: KeepSession, text: str) -> list[str]:
    with session.reader.connect() as conn:
        return [h.title for h in run_search(conn, SearchSpec(text=text), TagTree([], {}))]


def test_adding_fields_saves_them_in_keep_toml(session: KeepSession) -> None:
    session.add_own_field(PAGES)
    session.add_own_field(WHERE)
    assert list(load_keep_config(session.keep.toml_path).fields["books.book"]) == [
        "pages_read",
        "where_bought",
    ]
    with pytest.raises(OwnFieldError, match="already a field named"):
        session.add_own_field(PAGES)
    with pytest.raises(OwnFieldError, match="theme's fields"):
        session.add_own_field(OwnField("books.book", "pub", "Publisher"))
    with pytest.raises(OwnFieldError, match="no type"):
        session.add_own_field(OwnField("books.nothing", "x", "X"))


def test_edits_store_typed_values_with_undo(session: KeepSession) -> None:
    for own in (PAGES, FINISHED, WHERE, OWNED):
        session.add_own_field(own)
    dune = _id(session, "Dune")
    change = session.tags.edit_field(dune, PAGES.key, 120)
    assert change.label == "Set Pages read of 'Dune' to 120"
    session.tags.edit_field(dune, FINISHED.key, date(2026, 10, 1))
    session.tags.edit_field(dune, WHERE.key, "  Powell's  ")
    session.tags.edit_field(dune, OWNED.key, True)
    assert _rows(session) == [
        (dune, "finished", None, None, None, date(2026, 10, 1)),
        (dune, "owned", None, 1, None, None),
        (dune, "pages_read", None, 120, None, None),
        (dune, "where_bought", "Powell's", None, None, None),
    ]
    with session.reader.connect() as conn:
        sources = dict(
            conn.execute(
                select(FieldProvenance.field, FieldProvenance.source).where(
                    FieldProvenance.entity_id == dune
                )
            ).all()
        )
    assert sources[PAGES.key] == FieldSource.USER
    with pytest.raises(FieldEditError, match="whole number"):
        session.tags.edit_field(dune, PAGES.key, "lots")

    session.tags.undo()  # owned
    session.tags.undo()  # where bought
    assert [r[1] for r in _rows(session)] == ["finished", "pages_read"]
    session.tags.redo()
    assert "where_bought" in [r[1] for r in _rows(session)]
    session.tags.edit_field(dune, PAGES.key, None)  # emptied: no row
    assert "pages_read" not in [r[1] for r in _rows(session)]
    session.tags.undo()
    assert "pages_read" in [r[1] for r in _rows(session)]


def test_an_edit_of_a_field_the_type_hasnt_is_refused(session: KeepSession) -> None:
    dune = _id(session, "Dune")
    with pytest.raises(FieldEditError, match="removed"):
        session.tags.edit_field(dune, "own:nothing", 1)


def test_text_values_are_searchable(session: KeepSession) -> None:
    session.add_own_field(WHERE)
    session.tags.edit_field(_id(session, "Emma"), WHERE.key, "Blackwell's Oxford")
    assert _search(session, "blackwell") == ["Emma"]
    session.tags.undo()
    assert _search(session, "blackwell") == []


def test_the_page_lists_them_after_the_themes(session: KeepSession) -> None:
    session.add_own_field(PAGES)
    session.add_own_field(OWNED)
    dune = _id(session, "Dune")
    session.tags.edit_field(dune, PAGES.key, 99)
    with session.reader.connect() as conn:
        detail = load_detail(conn, session.schema, dune, lambda _: None, session.own_fields)
    assert detail is not None
    fields = next(s for s in detail.sections if s.kind == "fields").fields
    assert [f.name for f in fields][-2:] == ["own:pages_read", "own:owned"]
    pages, owned = fields[-2:]
    assert (pages.label, pages.value, pages.type, pages.edited, pages.editable) == (
        "Pages read",
        99,
        int,
        True,
        True,
    )
    assert (owned.value, owned.type, owned.edited) == (None, bool, False)


def test_removing_a_field_puts_its_values_in_extra_fields(session: KeepSession) -> None:
    session.add_own_field(PAGES)
    session.add_own_field(WHERE)
    dune, emma = _id(session, "Dune"), _id(session, "Emma")
    session.tags.edit_field(dune, PAGES.key, 120)
    session.tags.edit_field(emma, PAGES.key, 40)
    session.tags.edit_extra(emma, "Pages read", "a few", new=True)  # taken: the name says
    session.tags.edit_field(dune, WHERE.key, "Kobo")
    assert session.count_own_values(PAGES) == 2

    assert session.remove_own_field(PAGES) == 2
    assert list(session.own_fields["books.book"]) == ["where_bought"]
    assert list(load_keep_config(session.keep.toml_path).fields["books.book"]) == ["where_bought"]
    with session.reader.connect() as conn:
        extras: dict[int, object] = {i: e for i, e in conn.execute(select(Entity.id, Entity.extra))}
        left = conn.scalars(select(EntityField.field)).all()
        sources = conn.scalars(select(FieldProvenance.field)).all()
    assert extras[dune] == {"Pages read": "120"}
    assert extras[emma] == {"Pages read": "a few", "Pages read (pages_read)": "40"}
    assert left == ["where_bought"]
    assert PAGES.key not in sources
    assert _search(session, "120") == ["Dune"]
    # Its edits left the history: undoing goes to the edit before them.
    assert session.tags.undo_label == "Set Where bought of 'Dune' to 'Kobo'"


def test_changing_a_field_keeps_its_name_and_kind(session: KeepSession) -> None:
    session.add_own_field(PAGES)
    session.change_own_field(changed(PAGES, label="Pages so far", search="none", card=True))
    saved = load_keep_config(session.keep.toml_path).fields["books.book"]["pages_read"]
    assert (saved.label, saved.search, saved.card) == ("Pages so far", "none", True)
    with pytest.raises(OwnFieldError, match="kind"):
        session.change_own_field(OwnField("books.book", "pages_read", "P", "text"))
    with pytest.raises(OwnFieldError, match="theme's fields"):
        session.change_own_field(changed(PAGES, label="Year"))


def test_a_later_theme_field_of_the_same_name_is_noted(session: KeepSession) -> None:
    mine = OwnField("books.book", "year", "When read", "integer")
    # As if written before the theme had a Year field:
    session.save_config(replace(session.keep.config, fields={"books.book": {"year": mine}}))
    assert session.theme_clash(mine) == "Year"
    dune = _id(session, "Dune")
    session.tags.edit_field(dune, mine.key, 2001)  # both keep their values
    session.change_own_field(changed(mine, column=True))  # the clash it had stays allowed
    with session.reader.connect() as conn:
        detail = load_detail(conn, session.schema, dune, lambda _: None, session.own_fields)
    assert detail is not None
    fields = {f.name: f.value for s in detail.sections for f in s.fields}
    assert (fields["year"], fields["own:year"]) == (1965, 2001)


def test_merging_carries_values_and_undo_restores_them(session: KeepSession) -> None:
    session.add_own_field(PAGES)
    session.add_own_field(WHERE)
    dune, emma = _id(session, "Dune"), _id(session, "Emma")
    session.tags.edit_field(dune, PAGES.key, 120)
    session.tags.edit_field(emma, PAGES.key, 40)
    session.tags.edit_field(emma, WHERE.key, "Kobo")
    with session.reader.connect() as conn:
        plan = plan_merge(conn, session.schema, dune, [emma])
    conflict = next(c for c in plan.conflicts if c.key == PAGES.key)
    assert conflict.label == "Pages read"
    assert [c.text for c in conflict.choices] == ["120", "40"]

    def merge(conn: Connection) -> MergeChange:
        return merge_items(conn, session.schema, dune, [emma], {PAGES.key: emma})

    change = session.writer.run(merge)
    assert _rows(session) == [
        (dune, "pages_read", None, 40, None, None),
        (dune, "where_bought", "Kobo", None, None, None),
    ]
    session.writer.run(lambda conn: restore_merge(conn, session.schema, change, forward=False))
    assert _rows(session) == [
        (dune, "pages_read", None, 120, None, None),
        (emma, "pages_read", None, 40, None, None),
        (emma, "where_bought", "Kobo", None, None, None),
    ]
