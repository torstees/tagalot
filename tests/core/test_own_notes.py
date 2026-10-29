"""The keep's own fields in notes and Markdown front matter (#584): read into the fields
(by name or label), a value not of the kind kept as an Extra field, the user's values
kept, notes read again when the fields change, and Write to file… and Save as note…
writing them."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, FieldProvenance, FieldSource
from tagalot.core.notes import NoteText, map_note
from tagalot.core.own_fields import OwnField
from tagalot.core.own_notes import take_own
from tagalot.core.own_values import raw_values
from tagalot.core.root_admin import with_notes_folder, with_notes_writing
from tagalot.core.save_note import plan_note
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.writeback import own_changes, plan_write_back

BOOK, AUTHOR = "books.book", "books.author"
PAGES = OwnField(BOOK, "pages", "Pages read", "integer")
FINISHED = OwnField(BOOK, "finished", "Finished", "date")
SHOP = OwnField(BOOK, "shop", "Shop")
BORN = OwnField(AUTHOR, "born", "Born", "integer")


def test_map_note_fills_own_fields_by_name_or_label() -> None:
    note = NoteText(
        {"title": "Dune", "Pages-Read": 412, "finished": "soon", "shop": "Kobo", "mood": "x"},
        "Body",
    )
    found = map_note(note, [], own=[PAGES, FINISHED])
    assert found.own == {"pages": 412}
    assert found.extra == {"finished": "soon", "shop": "Kobo", "mood": "x"}
    assert found.problems == ["finished: isn't a date (YYYY-MM-DD), so it stays an Extra field"]
    assert map_note(note, [], ["pages-read"], own=[PAGES]).own == {}  # an ignored key


def test_take_own_splits_values() -> None:
    mine, rest, problems = take_own(
        {"pages": "1,024", "finished": date(2026, 1, 2), "other": 3, "shop": ""}, [PAGES, FINISHED]
    )
    assert mine == {"pages": 1024, "finished": date(2026, 1, 2)}
    assert rest == {"other": 3, "shop": ""}
    assert problems == []


@pytest.fixture
def keep(tmp_path: Path) -> Iterator[tuple[KeepSession, Path]]:
    files = tmp_path / "files"
    (files / "Notes" / "Authors").mkdir(parents=True)
    (files / "story.md").write_text(
        "---\ntitle: The Story\nauthor: Ann Writer\npages: 412\nfinished: soon\nshop: Kobo\n"
        "---\nIt was a dark night.\n",
        encoding="utf-8",
    )
    (files / "Notes" / "Authors" / "Ann Writer.md").write_text(
        "---\nborn: 1950\n---\nShe writes.\n", encoding="utf-8"
    )
    (files / "plain.epub").write_bytes(b"not a real epub")
    keep_dir = tmp_path / "Books.keep"
    create_keep(keep_dir, "Books", ThemeRef("books", 1), [RootConfig("r", "Files", str(files))])
    with KeepSession.open(keep_dir, Settings()) as session:
        config = with_notes_folder(session.keep.config, "r", "Notes")
        session.save_config(with_notes_writing(config, writable=True))
        for own in (PAGES, FINISHED, BORN):
            session.add_own_field(own)
        session.scan_all()
        yield session, files


def _id(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None, title
    return found


def _values(session: KeepSession, title: str) -> dict[str, Any]:
    with session.reader.connect() as conn:
        return raw_values(conn, [_id(session, title)]).get(_id(session, title), {})


def _source(session: KeepSession, title: str, key: str) -> FieldSource | None:
    with session.reader.connect() as conn:
        return conn.scalar(
            select(FieldProvenance.source).where(
                FieldProvenance.entity_id == _id(session, title), FieldProvenance.field == key
            )
        )


def _extra(session: KeepSession, title: str) -> dict[str, Any]:
    with session.reader.connect() as conn:
        return dict(conn.scalar(select(Entity.extra).where(Entity.title == title)) or {})


def test_a_scan_reads_them_from_files_and_notes(keep: tuple[KeepSession, Path]) -> None:
    session, _ = keep
    assert _values(session, "The Story") == {"pages": 412}
    assert _source(session, "The Story", PAGES.key) == FieldSource.EXTRACTED
    assert _extra(session, "The Story") == {"finished": "soon", "shop": "Kobo"}
    assert _values(session, "Ann Writer") == {"born": 1950}
    assert _source(session, "Ann Writer", BORN.key) == FieldSource.NOTE
    problems = [p.message for p in session.problems.items()]
    assert any("'The Story'" in p and "finished" in p for p in problems), problems


def test_the_users_value_stays_and_a_field_added_later_is_read(
    keep: tuple[KeepSession, Path],
) -> None:
    session, files = keep
    story = _id(session, "The Story")
    session.tags.edit_field(story, PAGES.key, 500)
    text = (files / "story.md").read_text(encoding="utf-8").replace("pages: 412", "pages: 600")
    (files / "story.md").write_text(text, encoding="utf-8")
    session.add_own_field(SHOP)  # the next scan reads every note and Markdown file again
    session.scan_all()
    assert _values(session, "The Story") == {"pages": 500, "shop": "Kobo"}
    assert _source(session, "The Story", SHOP.key) == FieldSource.EXTRACTED
    assert _extra(session, "The Story") == {"finished": "soon"}


def test_a_field_relabelled_reads_its_new_label(keep: tuple[KeepSession, Path]) -> None:
    session, files = keep
    (files / "Notes" / "Authors" / "Ann Writer.md").write_text(
        "---\nyear of birth: 1951\n---\nShe writes.\n", encoding="utf-8"
    )
    session.change_own_field(replace(BORN, label="Year of birth"))
    session.scan_all()
    assert _values(session, "Ann Writer") == {"born": 1951}


def test_write_to_file_writes_the_users_values(keep: tuple[KeepSession, Path]) -> None:
    session, _ = keep
    story = _id(session, "The Story")
    session.tags.edit_field(story, PAGES.key, 500)
    with session.reader.connect() as conn:
        plan = plan_write_back(
            conn,
            session.schema,
            session.tag_cache.get(),
            [story],
            {"r": session.root_path("r")},
            {"r"},
            own_fields=session.own_fields,
        )
    [write] = plan.files
    assert "pages: 500\n" in write.after
    assert "pages: 412" not in write.after


def test_own_changes_use_the_files_key() -> None:
    current = {"Pages Read": 3, "title": "x"}
    assert own_changes(current, {"pages": ("Pages read", 5)}) == {"Pages Read": 5}
    assert own_changes(current, {"pages": ("Pages read", 3)}) == {}
    assert own_changes({}, {"shop": ("Shop", "Kobo")}) == {"shop": "Kobo"}
    assert own_changes({}, {"shop": ("Shop", None)}) == {}


def test_save_as_note_includes_them(keep: tuple[KeepSession, Path]) -> None:
    session, _ = keep
    plain = _id(session, "plain")
    session.tags.edit_field(plain, PAGES.key, 99)
    session.tags.edit_field(plain, FINISHED.key, date(2026, 10, 1))
    session.tags.edit_note(plain, "Typed.")
    with session.reader.connect() as conn:
        note = plan_note(
            conn,
            session.schema,
            session.tag_cache.get(),
            session.keep.config,
            session.root_path,
            plain,
        )
    assert "pages: 99\n" in note.text
    assert "finished: 2026-10-01\n" in note.text
