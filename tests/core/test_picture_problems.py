"""Triage's Pictures list (#426): covers a Markdown book names that weren't found, with the
paths tried and what is there now, and pictures that can't be drawn."""

import os
from datetime import timedelta

from sqlalchemy import delete, select, update

from tagalot.builtin_themes.books import Book
from tagalot.core.assets import assets_folders
from tagalot.core.models import Entity, PictureFailure, Resource
from tagalot.core.picture_problems import (
    CANT_DRAW,
    HERE,
    ITEM,
    MISSING_NOW,
    NOT_FOUND,
    NOT_SCANNED,
    NOT_THERE,
    TYPE,
    UNREACHABLE,
    VALUE,
    PictureIgnoreChange,
    PictureProblem,
    PictureScope,
    count_picture_problems,
    ignore_pictures,
    picture_problems,
    restore_picture_ignores,
    stop_ignoring,
)
from tests.core.book_files import jpeg, write_markdown
from tests.core.test_thumb_resolve import Env as ThumbEnv
from tests.core.test_thumb_resolve import env as thumb_env
from tests.core.test_thumb_resolve import write_image
from tests.themes.test_books import T0, Env, _covers, env

__all__ = ["env", "thumb_env"]  # fixtures


def _problems(env: Env | ThumbEnv) -> list[PictureProblem]:
    env.writer.run(lambda conn: None)  # failures are queued; let them land
    with env.reader.connect() as conn:
        found = picture_problems(conn, lambda root: str(env.files) if root == "r" else None)
        assert count_picture_problems(conn) == len(found)
    return found


def _story(env: Env, front: str) -> None:
    write_markdown(env.files / "Stories/story.md", f"---\ntitle: Story\n{front}---\nText.\n")


def test_a_cover_not_found_lists_the_paths_tried(env: Env) -> None:
    _story(env, "cover: covers/c.jpg\nassets: Art\n")
    env.assets, env.keep_assets = "Shared", "./Local"
    env.scan()
    [problem] = _problems(env)
    assert (problem.kind, problem.title, problem.role) == (NOT_FOUND, "Story", "cover")
    assert (problem.relpath, problem.written) == ("Stories/story.md", "covers/c.jpg")
    assert [(t.relpath, t.state) for t in problem.tried] == [
        ("Stories/covers/c.jpg", NOT_THERE),  # beside the file
        ("Stories/Art/covers/c.jpg", NOT_THERE),  # its own assets folder
        ("Shared/covers/c.jpg", NOT_THERE),  # the watched folder's
        ("Stories/Local/covers/c.jpg", NOT_THERE),  # the keep's
    ]
    assert problem.summary == "covers/c.jpg: not found"


def test_each_path_says_what_is_there_now(env: Env) -> None:
    _story(env, "cover: c.jpg\nassets: Art\n")
    env.scan()
    (env.files / "Stories/Art").mkdir(parents=True)
    (env.files / "Stories/Art/c.jpg").write_bytes(jpeg())
    [problem] = _problems(env)  # added since the scan
    assert [t.state for t in problem.tried] == [NOT_THERE, NOT_SCANNED]

    env.scan(T0 + timedelta(minutes=1))  # scanned, but the book wasn't read again
    [problem] = _problems(env)
    assert [t.state for t in problem.tried] == [NOT_THERE, HERE]
    assert problem.tried[1].found
    assert problem.summary == "c.jpg: here now; re-read to use it"

    env.writer.run(lambda conn: conn.execute(update(Resource).values(ingested_at=None)))
    env.scan(T0 + timedelta(minutes=2))  # as Re-read from file does
    assert _covers(env, "Story") == ["Stories/Art/c.jpg"]
    assert _problems(env) == []

    (env.files / "Stories/Art/c.jpg").unlink()
    env.scan(T0 + timedelta(minutes=3))
    assert _covers(env, "Story") == ["Stories/Art/c.jpg"]  # kept, though it's missing
    assert _problems(env) == []


def test_a_missing_file_and_an_unreachable_folder(env: Env) -> None:
    (env.files / "Stories").mkdir(parents=True)
    (env.files / "Stories/other.jpg").write_bytes(jpeg())
    _story(env, "cover: c.jpg\nassets: Art\n")
    env.scan()
    env.writer.run(
        lambda conn: conn.execute(
            update(Resource)
            .where(Resource.relpath == "Stories/other.jpg")
            .values(relpath="Stories/Art/c.jpg", status="missing")
        )
    )  # as if it had been there and gone
    [problem] = _problems(env)
    assert [t.state for t in problem.tried] == [NOT_THERE, MISSING_NOW]
    with env.reader.connect() as conn:
        [far] = picture_problems(conn, lambda root: None)
    assert [t.state for t in far.tried] == [UNREACHABLE, MISSING_NOW]


def test_a_cover_found_later_beside_the_file_clears_it(env: Env) -> None:
    _story(env, "cover: c.jpg\n")
    env.scan()
    assert len(_problems(env)) == 1
    (env.files / "Stories/c.jpg").write_bytes(jpeg())
    env.scan(T0 + timedelta(minutes=1))  # the picture is read after its book: linked
    assert _covers(env, "Story") == ["Stories/c.jpg"]
    assert _problems(env) == []


def test_a_web_address_or_a_drives_path_says_why(env: Env) -> None:
    _story(env, "cover: https://example.com/c.jpg\n")
    env.scan()
    [problem] = _problems(env)
    assert problem.tried == ()
    assert problem.written == "https://example.com/c.jpg"
    assert problem.reason is not None
    assert "web address" in problem.reason
    _story(env, "cover: 'C:\\Pictures\\c.jpg'\n")
    env.scan(T0 + timedelta(minutes=1))
    [problem] = _problems(env)
    assert problem.reason is not None
    assert "drive" in problem.reason
    _story(env, "cover: ../../outside.jpg\n")
    env.scan(T0 + timedelta(minutes=2))
    [problem] = _problems(env)
    assert problem.reason == "It isn't a path inside the watched folder."


def test_a_file_that_stops_naming_it_or_a_deleted_book_clears_it(env: Env) -> None:
    _story(env, "cover: c.jpg\n")
    env.scan()
    assert len(_problems(env)) == 1
    _story(env, "")
    env.scan(T0 + timedelta(minutes=1))
    assert _problems(env) == []
    _story(env, "cover: d.jpg\n")
    env.scan(T0 + timedelta(minutes=2))
    [problem] = _problems(env)
    assert problem.written == "d.jpg"
    env.writer.run(lambda conn: conn.execute(delete(Entity).where(Entity.id == problem.entity_id)))
    assert _problems(env) == []


def test_a_picture_that_cant_be_drawn(thumb_env: ThumbEnv) -> None:
    env = thumb_env
    (env.files / "beach.png").write_bytes(b"not an image")
    os.utime(env.files / "beach.png", ns=(1, 1_800_000_000_000_000_000))
    env.scan(T0 + timedelta(hours=1))
    photo = env.entity("beach.png")
    env.resolver().resolve(photo)
    [problem] = _problems(env)
    assert (problem.kind, problem.entity_id, problem.relpath) == (CANT_DRAW, photo, "beach.png")
    assert problem.reason
    assert problem.summary.startswith("Can't be drawn: ")

    write_image(env.files / "beach.png")  # fixed: a changed file isn't listed
    os.utime(env.files / "beach.png", ns=(1, 1_900_000_000_000_000_000))
    env.scan(T0 + timedelta(hours=2))
    assert _problems(env) == []
    with env.reader.connect() as conn:
        assert conn.scalar(select(PictureFailure.resource_id)) is not None  # until drawn
    assert env.resolver().resolve(photo).thumbnail is not None
    env.writer.run(lambda conn: None)
    with env.reader.connect() as conn:
        assert conn.scalar(select(PictureFailure.resource_id)) is None


# --- ignoring (#426) ---


def _ignore(env: Env, problems: list[PictureProblem], scope: PictureScope) -> PictureIgnoreChange:
    return env.writer.run(lambda conn: ignore_pictures(conn, problems, scope, "Ignore"))


def _listed(env: Env) -> dict[str, str | None]:
    with env.reader.connect() as conn:
        found = picture_problems(conn, lambda root: None, ignored=True)
    return {p.title: p.ignored for p in found}


def test_ignoring_a_row_for_its_item_until_it_changes(env: Env) -> None:
    _story(env, "cover: c.jpg\n")
    write_markdown(env.files / "Stories/other.md", "---\ntitle: Other\ncover: c.jpg\n---\n")
    env.scan()
    story = next(p for p in _problems(env) if p.title == "Story")
    change = _ignore(env, [story], ITEM)
    assert [p.title for p in _problems(env)] == ["Other"]
    assert _listed(env) == {"Other": None, "Story": ITEM}
    env.writer.run(lambda conn: restore_picture_ignores(conn, change, forward=False))
    assert [p.title for p in _problems(env)] == ["Other", "Story"]  # undone
    env.writer.run(lambda conn: restore_picture_ignores(conn, change, forward=True))
    _story(env, "cover: d.jpg\n")
    env.scan(T0 + timedelta(minutes=1))
    assert [p.title for p in _problems(env)] == ["Other", "Story"]  # it says something else


def test_ignoring_a_value_everywhere_or_a_type(env: Env) -> None:
    _story(env, "cover: TBD\n")
    write_markdown(env.files / "Stories/other.md", "---\ntitle: Other\ncover: tbd\n---\n")
    write_markdown(env.files / "Stories/third.md", "---\ntitle: Third\ncover: x.jpg\n---\n")
    env.scan()
    story = next(p for p in _problems(env) if p.title == "Story")
    _ignore(env, [story], VALUE)
    assert [p.title for p in _problems(env)] == ["Third"]  # case aside
    assert _listed(env) == {"Other": VALUE, "Story": VALUE, "Third": None}
    [third] = _problems(env)
    _ignore(env, [third], TYPE)
    assert _problems(env) == []
    assert _listed(env)["Third"] == TYPE

    with env.reader.connect() as conn:
        shown = picture_problems(conn, lambda root: None, ignored=True)
    again = next(p for p in shown if p.title == "Story")
    change = env.writer.run(lambda conn: stop_ignoring(conn, again, "Stop"))
    assert {r.scope for r in change.removed} == {VALUE, TYPE}  # every rule hiding it
    assert {p.title for p in _problems(env)} == {"Story", "Other", "Third"}


def test_placeholders_are_no_value(env: Env) -> None:
    for i, value in enumerate(("None", "null", "N/A", "'-'", "'[[]]'")):
        _story(env, f"cover: {value}\nassets: None\n")
        env.scan(T0 + timedelta(minutes=i))
        assert _problems(env) == [], value
        assert env.fields(Book, "Story")["assets"] is None
    assert assets_folders("a/b.md", "None", ["null", "Shared"]) == ["Shared"]
