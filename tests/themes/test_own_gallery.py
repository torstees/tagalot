"""A book's own folder as its Pictures gallery (#448): the core fills ``own_folder`` roles."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tagalot.builtin_themes import books
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import Theme, role
from tagalot.themes.loader import validate_theme
from tests.core.media_files import write_image
from tests.themes.test_pictures import Env, make_env, write_zip


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    yield from make_env(tmp_path, books.BooksTheme)


def pictures(env: Env, title: str) -> list[str]:
    return sorted(env.files_of(title).get("pictures", []))


def story(files: Path, folder: str, title: str, front: str = "") -> None:
    path = files / folder / f"{title}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{front}# {title}\n", encoding="utf-8")


def test_a_storys_renders_are_its_gallery(env: Env) -> None:
    story(env.files, "Writing/Story X", "Story X")
    write_image(env.files / "Writing/Story X/renders/hero.png")
    write_image(env.files / "Writing/Story X/renders/old/ship.jpg")  # at any depth
    write_zip(env.files / "Writing/Story X/sketches.zip", [("s.png", (2015, 3, 4, 0, 0, 0))])
    env.scan()
    assert pictures(env, "Story X") == [
        "Writing/Story X/renders/hero.png",
        "Writing/Story X/renders/old/ship.jpg",
        "Writing/Story X/sketches.zip",
    ]
    # They're still pictures and archives of their own (#446).
    assert env.titles(books.Picture) == ["hero.png", "ship.jpg"]


def test_only_a_folder_with_one_work_is_its_own(env: Env) -> None:
    story(env.files, "Writing", "Other")  # Writing holds Story X's folder too: no one's
    story(env.files, "Writing/Story X", "Story X")
    story(env.files, "Pair", "One")
    story(env.files, "Pair", "Two")
    story(env.files, "", "At the top")  # the top of a watched folder is never one
    for folder in ("Writing", "Writing/Story X", "Pair", ""):
        write_image(env.files / folder / "art.png")
    env.scan()
    assert pictures(env, "Story X") == ["Writing/Story X/art.png"]
    assert pictures(env, "Other") == []
    assert pictures(env, "One") == pictures(env, "Two") == []
    assert pictures(env, "At the top") == []


def test_its_cover_and_a_comic_beside_it_are_not_its_pictures(env: Env) -> None:
    story(env.files, "Tale", "Tale", "---\ncover: cover.png\n---\n")
    write_image(env.files / "Tale/cover.png")
    write_image(env.files / "Tale/map.png")
    write_zip(env.files / "Tale/Tale 1.cbz", [("01.jpg", (2015, 3, 4, 0, 0, 0))])  # a comic
    env.scan()
    assert env.files_of("Tale")["cover"] == ["Tale/cover.png"]
    assert pictures(env, "Tale") == ["Tale/map.png"]


def test_the_gallery_follows_the_folder(env: Env) -> None:
    story(env.files, "Tale", "Tale")
    write_image(env.files / "Tale/a.png")
    env.scan()
    assert pictures(env, "Tale") == ["Tale/a.png"]
    os.remove(env.files / "Tale/a.png")
    write_image(env.files / "Tale/b.png")
    env.scan()
    assert pictures(env, "Tale") == ["Tale/b.png"]
    story(env.files, "Tale", "Second")  # now the folder holds two works: no one's own
    env.scan()
    assert pictures(env, "Tale") == []


class _Thing(ThemeEntity):
    roles = [
        role("file", kinds={"image"}, primary=True),
        role("loose", kinds={"image"}, many=True, own_folder=True),
        role("both", kinds={"image"}, many=True, gallery=True, own_folder=True, folders=("A",)),
    ]


class _OldTheme(Theme):
    id, name, version = "old_own", "Old", 1
    api_version = 18
    extensions = frozenset({".png"})
    entities = [_Thing]

    def ingest(self, batch: object, ctx: object) -> None:
        pass


def test_own_folder_galleries_are_checked() -> None:
    problems = validate_theme(_OldTheme)
    assert "_Thing.loose: own_folder needs gallery=True" in problems
    assert "_Thing.both: own_folder and folders can't be used together" in problems
    assert "_Thing.both: own_folder needs api_version = 19 (or later)" in problems
