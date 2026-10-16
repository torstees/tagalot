"""Folder items (#447): folders of pictures hold what's in them, made by the core."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import aliased

from tagalot.builtin_themes import books, movies
from tagalot.core.containers import uncontain_by_hand
from tagalot.core.models import Entity, EntityAncestor, EntityContains
from tagalot.core.tags import add_tag, tag_entities
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import Theme, contains, role
from tagalot.themes.loader import validate_theme
from tests.core.media_files import write_image
from tests.themes.test_pictures import Env, make_env, video, write_zip


@pytest.fixture
def movies_env(tmp_path: Path) -> Iterator[Env]:
    yield from make_env(tmp_path, movies.MoviesTheme)


@pytest.fixture
def books_env(tmp_path: Path) -> Iterator[Env]:
    yield from make_env(tmp_path, books.BooksTheme)


def contents(env: Env) -> dict[str, list[str]]:
    """Each Folder's contents, by title."""
    parent, child = aliased(Entity), aliased(Entity)
    folder = env.theme.type_id_of(
        movies.Folder if env.theme is movies.MoviesTheme else books.Folder
    )
    with env.reader.connect() as conn:
        rows = conn.execute(
            select(parent.title, child.title)
            .join(EntityContains, EntityContains.parent_id == parent.id)
            .join(child, child.id == EntityContains.child_id)
            .where(parent.type == folder)
        ).all()
    found: dict[str, list[str]] = {}
    for title, inside in rows:
        found.setdefault(title, []).append(inside)
    return {a: sorted(v) for a, v in sorted(found.items())}


def item(env: Env, title: str) -> int:
    with env.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None, title
    return found


def ancestors(env: Env, title: str) -> list[str]:
    above = aliased(Entity)
    with env.reader.connect() as conn:
        return sorted(
            conn.scalars(
                select(above.title)
                .join(EntityAncestor, EntityAncestor.ancestor_id == above.id)
                .where(EntityAncestor.entity_id == item(env, title), EntityAncestor.depth > 0)
            )
        )


def home_videos(files: Path) -> None:
    video(files / "Home/Alice/beach.mp4")
    video(files / "Home/Alice/park.mp4")
    write_image(files / "Home/Alice/IMG_1.jpg")
    write_zip(files / "Home/Alice/scans.zip", [("a.jpg", (1998, 1, 1, 0, 0, 0))])
    video(files / "Home/Alice/3rd Birthday/party.mp4")  # one video: the movie's own folder
    write_image(files / "Home/Alice/3rd Birthday/cake.jpg")
    video(files / "Home/Bob/swim.mp4")  # no pictures: no folder of its own


def test_folders_above_pictures_hold_what_is_in_them(movies_env: Env) -> None:
    home_videos(movies_env.files)
    movies_env.scan()
    assert movies_env.titles(movies.Folder) == ["3rd Birthday", "Alice", "Home"]
    assert contents(movies_env) == {
        # A movie's own folder with pictures is a folder too, holding the movie (#447).
        "3rd Birthday": ["cake.jpg", "party"],
        "Alice": ["3rd Birthday", "IMG_1.jpg", "beach", "park", "scans.zip"],
        "Home": ["Alice", "swim"],  # Bob's folder has no pictures: Home holds his video
    }
    # A tag on Alice counts for everything under her folder (with inherit tags).
    assert ancestors(movies_env, "cake.jpg") == ["3rd Birthday", "Alice", "Home"]
    assert ancestors(movies_env, "party") == ["3rd Birthday", "Alice", "Home"]
    assert ancestors(movies_env, "swim") == ["Home"]


def test_a_picture_added_later_makes_its_folder(movies_env: Env) -> None:
    video(movies_env.files / "Trips/Iceland/day 1.mp4")
    video(movies_env.files / "Trips/Iceland/day 2.mp4")
    movies_env.scan()
    assert movies_env.titles(movies.Folder) == []
    write_image(movies_env.files / "Trips/Iceland/glacier.jpg")
    movies_env.scan()  # the videos aren't read again, but their folder holds them
    assert contents(movies_env) == {
        "Iceland": ["day 1", "day 2", "glacier.jpg"],
        "Trips": ["Iceland"],
    }


def test_a_folder_without_pictures_goes_unless_tagged(movies_env: Env) -> None:
    files = movies_env.files
    for name in ("Trips/Iceland", "Trips/Peru"):
        video(files / name / "a.mp4")
        video(files / name / "b.mp4")
        write_image(files / name / "p.jpg")
    movies_env.scan()
    assert movies_env.titles(movies.Folder) == ["Iceland", "Peru", "Trips"]
    tag = movies_env.writer.run(lambda conn: add_tag(conn, None, "Holiday"))
    peru = item(movies_env, "Peru")
    movies_env.writer.run(lambda conn: tag_entities(conn, [peru], [tag]))
    os.remove(files / "Trips/Iceland/p.jpg")
    os.remove(files / "Trips/Peru/p.jpg")
    movies_env.scan()
    # Iceland's and Trips' folders go; Peru's stays, with its tag, holding its videos.
    assert movies_env.titles(movies.Folder) == ["Peru"]
    assert contents(movies_env) == {"Peru": ["a", "b"]}
    assert item(movies_env, "Peru") == peru


def test_a_collection_folder_is_not_a_folder_item(movies_env: Env) -> None:
    files = movies_env.files
    for name in ("Fellowship (2001)", "Two Towers (2002)"):
        video(files / "Films/LOTR" / name / "movie.mkv")
    write_image(files / "Films/LOTR/map.jpg")
    movies_env.scan()
    assert movies_env.titles(movies.Collection) == ["LOTR"]
    assert movies_env.titles(movies.Folder) == ["Films"]  # not LOTR: it's the collection
    # The films stay the collection's; the map, which a collection doesn't hold, goes up.
    assert contents(movies_env) == {"Films": ["LOTR", "map.jpg"]}


def extra(env: Env, title: str) -> dict[str, object]:
    with env.reader.connect() as conn:
        found = conn.scalar(select(Entity.extra).where(Entity.title == title))
    return dict(found or {})


def test_a_folder_takes_its_note(movies_env: Env) -> None:
    files = movies_env.files
    home_videos(files)
    (files / "Home/Alice/Alice.md").write_text(
        "---\nborn: 2018-03-14\n---\nOur daughter.\n", encoding="utf-8"
    )
    (files / "Home/Alice/3rd Birthday/3rd Birthday.md").write_text(
        "---\ntitle: Alice's 3rd birthday\nplace: Grandma's garden\n---\nCake everywhere.\n",
        encoding="utf-8",
    )
    movies_env.scan()
    assert extra(movies_env, "Alice") == {"born": "2018-03-14"}
    # A folder note's title names the folder; in a movie's own folder the movie reads the
    # same note (its own, by ctx.note), keeping its title: a note only renames its folder.
    assert extra(movies_env, "Alice's 3rd birthday") == {"place": "Grandma's garden"}
    assert extra(movies_env, "party") == {"place": "Grandma's garden"}
    assert contents(movies_env)["Alice's 3rd birthday"] == ["cake.jpg", "party"]


def test_what_the_user_takes_out_stays_out(movies_env: Env) -> None:
    home_videos(movies_env.files)
    movies_env.scan()
    alice, beach = item(movies_env, "Alice"), item(movies_env, "beach")
    movies_env.writer.run(lambda conn: uncontain_by_hand(conn, movies_env.schema, alice, [beach]))
    write_image(movies_env.files / "Home/Alice/IMG_2.jpg")
    movies_env.scan()
    assert "beach" not in contents(movies_env)["Alice"]
    assert "IMG_2.jpg" in contents(movies_env)["Alice"]


def test_a_story_its_renders_and_the_folders_above(books_env: Env) -> None:
    files = books_env.files
    (files / "Writing/Story X").mkdir(parents=True)
    (files / "Writing/Story X/story.md").write_text("# Story X\n", encoding="utf-8")
    write_image(files / "Writing/Story X/renders/hero.png")
    (files / "Writing/Other.md").write_text("# Other\n", encoding="utf-8")
    (files / "Kobo").mkdir()
    (files / "Kobo/plain.md").write_text("# Plain\n", encoding="utf-8")  # no pictures
    books_env.scan()
    assert contents(books_env) == {
        "Story X": ["Story X", "renders"],
        "Writing": ["Other", "Story X"],
        "renders": ["hero.png"],
    }
    assert ancestors(books_env, "hero.png") == ["Story X", "Writing", "renders"]


class _Pic(ThemeEntity):
    roles = [role("file", kinds={"image"}, primary=True)]


class _Folder(ThemeEntity):
    folders_for = (_Pic,)
    roles = [role("file", kinds={"image"}, primary=True)]


class _BadFolders(Theme):
    id, name, version = "bad_folders", "Bad", 1
    api_version = 17
    extensions = frozenset({".png"})
    entities = [_Pic, _Folder]
    containment = [contains(_Folder, _Pic)]

    def ingest(self, batch: object, ctx: object) -> None:
        pass


def test_folder_items_are_checked() -> None:
    problems = validate_theme(_BadFolders)
    assert "_Folder: folder items need a primary role of kind 'dir'" in problems
    assert "_Folder: folder items need dirs = True" in problems
    assert "a theme with folders_for must set api_version = 18 (or later)" in problems
