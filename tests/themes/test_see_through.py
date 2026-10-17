"""See-through folders (#459): folder names in the keep's settings that are never items, the
files in them counting as their parent folder's."""

import dataclasses
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.builtin_themes import movies
from tagalot.core.keep import KeepConfig, ThemeRef, clean_names, dump_keep_config
from tagalot.core.keep import load_keep_config as load
from tagalot.core.models import Entity, Resource
from tagalot.core.see_through import counts_in, folder_of, hidden_dir, logical, names_of
from tagalot.core.tags import add_tag, tag_entities
from tagalot.core.thumbnails.cache import ThumbCache
from tagalot.core.thumbnails.resolve import ThumbnailResolver
from tagalot.themes.api import ResourceInfo, files_in
from tests.core.media_files import write_image
from tests.themes.test_folders import ancestors, contents, item
from tests.themes.test_pictures import Env, make_env, video

POSTERS = names_of(["! Posters"])


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    for found in make_env(tmp_path, movies.MoviesTheme):
        yield dataclasses.replace(found, see_through=POSTERS)


def note(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def files_of(env: Env, title: str, role: str) -> list[str]:
    return sorted(env.files_of(title).get(role, []))


# --- the paths ---


@pytest.mark.parametrize(
    ("relpath", "counted", "folder"),
    [
        ("A/b.jpg", None, "A"),
        ("A/! Posters/b.jpg", "A", "A"),
        ("A/! posters/b.jpg", "A", "A"),  # case aside
        ("A/! Posters/old/b.jpg", "A", "A"),  # at any depth
        ("! Posters/b.jpg", "", ""),  # at the top of the root
        ("A/B/! Posters/b.jpg", "A/B", "A/B"),
        ("A/! Postersx/b.jpg", None, "A/! Postersx"),  # matched exactly
    ],
)
def test_the_folder_a_file_counts_as_in(relpath: str, counted: str | None, folder: str) -> None:
    assert counts_in(relpath, POSTERS) == counted
    assert folder_of(relpath, POSTERS) == folder
    name = relpath.rpartition("/")[2]
    assert logical(relpath, POSTERS) == (f"{folder}/{name}" if folder else name)


def test_see_through_folders_and_those_in_them_are_hidden() -> None:
    assert hidden_dir("A/! Posters", POSTERS)
    assert hidden_dir("A/! Posters/old", POSTERS)
    assert not hidden_dir("A", POSTERS)
    assert not hidden_dir("A/! Posters", frozenset())


def test_a_resource_says_where_it_counts(tmp_path: Path) -> None:
    path = os.path.join(str(tmp_path), "A", "! Posters", "old", "b.jpg")
    info = ResourceInfo(1, "r", "A/! Posters/old/b.jpg", "file", ".jpg", 1, 1, path, "A", POSTERS)
    assert info.folder == "A"
    assert info.folder_path == os.path.join(str(tmp_path), "A")
    plain = ResourceInfo(2, "r", "A/b.jpg", "file", ".jpg", 1, 1, os.path.join("x", "A", "b.jpg"))
    assert (plain.folder, plain.folder_path) == ("A", os.path.join("x", "A"))


def test_files_in_a_folder_and_its_see_through_folders(tmp_path: Path) -> None:
    for name in ("a.jpg", "! Posters/b.jpg", "! Posters/old/c.jpg", "other/d.jpg"):
        write_image(tmp_path / name)
    assert files_in(str(tmp_path)) == ["a.jpg"]
    assert files_in(str(tmp_path), POSTERS) == ["a.jpg", "b.jpg", "c.jpg"]
    assert files_in(str(tmp_path / "missing"), POSTERS) == []


# --- keep.toml ---


def test_the_names_are_kept_in_keep_toml(tmp_path: Path) -> None:
    config = KeepConfig(uuid.uuid4(), "K", ThemeRef("movies", 1), see_through=["! Posters"])
    text = dump_keep_config(config)
    assert "[folders]\nsee_through = ['! Posters']" in text
    (tmp_path / "keep.toml").write_text(text, encoding="utf-8")
    assert load(tmp_path / "keep.toml").see_through == ["! Posters"]
    assert "[folders]" not in dump_keep_config(dataclasses.replace(config, see_through=[]))


def test_names_are_cleaned() -> None:
    assert clean_names([" ! Posters ", "", "! posters", "Extras/", "  "]) == ["! Posters", "Extras"]


# --- the movies theme ---


def test_a_see_through_folder_is_never_a_folder_item(env: Env) -> None:
    video(env.files / "Home/Alice/beach.mp4")
    video(env.files / "Home/Alice/park.mp4")
    write_image(env.files / "Home/Alice/! Posters/folder.jpg")
    write_image(env.files / "Home/Alice/! Posters/snap.jpg")
    write_image(env.files / "Home/Alice/! Posters/old/scan.jpg")
    env.scan()
    assert env.titles(movies.Folder) == ["Alice", "Home"]
    assert contents(env)["Alice"] == ["beach", "park", "scan.jpg", "snap.jpg"]
    assert ancestors(env, "snap.jpg") == ["Alice", "Home"]


def test_its_folder_image_is_its_parents(env: Env, tmp_path: Path) -> None:
    video(env.files / "Home/Alice/beach.mp4")
    video(env.files / "Home/Alice/park.mp4")
    write_image(env.files / "Home/Alice/! Posters/folder.jpg")
    write_image(env.files / "Home/Alice/IMG_1.jpg")
    env.scan()
    cache = ThumbCache(tmp_path / "thumbs.db")
    resolver = ThumbnailResolver(
        env.reader, env.writer, movies.MoviesTheme, cache, lambda _: str(env.files), 64
    )
    resolver.see_through = lambda: POSTERS
    result = resolver.resolve(item(env, "Alice"))
    cache.close()
    with env.reader.connect() as conn:
        source = conn.scalar(select(Resource.relpath).where(Resource.id == result.resource_id))
    assert source == "Home/Alice/! Posters/folder.jpg"


def test_a_movies_poster_and_nfo_in_its_see_through_folder(env: Env) -> None:
    video(env.files / "Heat (1995)/Heat.mkv")
    write_image(env.files / "Heat (1995)/! Posters/poster.jpg")
    write_image(env.files / "Heat (1995)/! Posters/still.jpg")
    note(
        env.files / "Heat (1995)/! Posters/movie.nfo",
        "<movie><title>Heat</title><plot>A heist.</plot></movie>",
    )
    env.scan()
    assert files_of(env, "Heat", "poster") == ["Heat (1995)/! Posters/poster.jpg"]
    assert files_of(env, "Heat", "screenshot") == ["Heat (1995)/! Posters/still.jpg"]
    assert files_of(env, "Heat", "nfo") == ["Heat (1995)/! Posters/movie.nfo"]
    assert env.titles(movies.Picture) == ["still.jpg"]  # the poster is the movie's


def test_a_folders_note_in_its_see_through_folder(env: Env) -> None:
    video(env.files / "Home/Alice/beach.mp4")
    video(env.files / "Home/Alice/park.mp4")
    write_image(env.files / "Home/Alice/IMG_1.jpg")
    note(env.files / "Home/Alice/! Posters/Alice.md", "---\npeople: [Alice]\n---\nOur girl.\n")
    env.scan()
    assert env.titles(movies.Person) == ["Alice"]
    assert "Alice" in ancestors(env, "IMG_1.jpg")  # the person Alice holds her folder


def test_naming_a_folder_later_takes_its_folder_item_away(env: Env) -> None:
    plain = dataclasses.replace(env, see_through=frozenset())
    video(env.files / "Home/Alice/beach.mp4")
    video(env.files / "Home/Alice/park.mp4")
    write_image(env.files / "Home/Alice/! Posters/snap.jpg")
    write_image(env.files / "Home/Alice/! Posters/old/scan.jpg")
    plain.scan()
    assert plain.titles(movies.Folder) == ["! Posters", "Alice", "Home", "old"]
    tag = env.writer.run(lambda conn: add_tag(conn, None, "Kept"))
    posters = item(env, "! Posters")
    env.writer.run(lambda conn: tag_entities(conn, [posters], [tag]))
    env.scan()  # now see-through: even the tagged one goes
    assert env.titles(movies.Folder) == ["Alice", "Home"]
    assert contents(env)["Alice"] == ["beach", "park", "scan.jpg", "snap.jpg"]
    with env.reader.connect() as conn:
        assert conn.scalar(select(Entity.id).where(Entity.id == posters)) is None
    plain.scan()  # and back
    assert plain.titles(movies.Folder) == ["! Posters", "Alice", "Home", "old"]
