"""People in the movies theme (#449): actors shown as people, people in pictures, and a
folder's note naming the people it is about."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import aliased

from tagalot.builtin_themes import movies
from tagalot.core.models import Entity, EntityAncestor, EntityContains
from tagalot.core.notes_folder import note_type, type_names
from tagalot.core.theme_schema import build_theme_schema
from tests.core.media_files import write_image
from tests.themes.test_folders import contents, home_videos, item
from tests.themes.test_pictures import Env, make_env, video


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    yield from make_env(tmp_path, movies.MoviesTheme)


def note(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def holders(env: Env, title: str) -> list[str]:
    """The people holding an item, at any depth."""
    person, above = movies.MoviesTheme.type_id_of(movies.Person), aliased(Entity)
    with env.reader.connect() as conn:
        return sorted(
            conn.scalars(
                select(above.title)
                .join(EntityAncestor, EntityAncestor.ancestor_id == above.id)
                .where(EntityAncestor.entity_id == item(env, title), above.type == person)
            )
        )


def test_actors_are_people_stored_as_before() -> None:
    assert movies.MoviesTheme.type_id_of(movies.Person) == "movies.actor"
    schema = build_theme_schema(movies.MoviesTheme)
    assert schema.entities[movies.Person].table.name == "movies_actor"
    assert "pictured" in schema.relationships


def test_notes_folders_named_for_actors_still_find_people() -> None:
    names = type_names(build_theme_schema(movies.MoviesTheme))
    for folder in ("Actors", "Actor", "People", "Person"):
        assert note_type(names, None, folder) == ("movies.actor", None)
    assert note_type(names, "actor", "") == ("movies.actor", None)


def test_a_folder_note_names_the_people_it_is_about(env: Env) -> None:
    home_videos(env.files)
    note(env.files / "Home/Alice/Alice.md", "---\npeople: [Alice]\n---\nOur daughter.\n")
    note(
        env.files / "Home/Alice/3rd Birthday/3rd Birthday.md",
        "---\npeople: Alice, Grandma\n---\n",
    )
    env.scan()
    assert env.titles(movies.Person) == ["Alice", "Grandma"]
    assert contents(env)["Alice"] == ["3rd Birthday", "IMG_1.jpg", "beach", "park", "scans.zip"]
    # Alice holds her folder, so everything under it is within her; Grandma the party's.
    assert holders(env, "IMG_1.jpg") == ["Alice"]
    assert holders(env, "cake.jpg") == ["Alice", "Grandma"]
    assert holders(env, "party") == ["Alice", "Grandma"]
    assert holders(env, "swim") == []


def test_a_person_dropped_from_the_note_lets_go(env: Env) -> None:
    home_videos(env.files)
    path = env.files / "Home/Alice/3rd Birthday/3rd Birthday.md"
    note(path, "---\npeople: Alice, Grandma\n---\n")
    env.scan()
    note(path, "---\npeople: Alice\n---\nNow without Grandma.\n")
    env.scan()
    assert holders(env, "cake.jpg") == ["Alice"]
    assert "Grandma" in env.titles(movies.Person)  # she stays a person


def test_a_folder_notes_person_is_the_nfos_actor(env: Env) -> None:
    video(env.files / "Heat (1995)/Heat.mkv")
    note(
        env.files / "Heat (1995)/movie.nfo",
        "<movie><title>Heat</title><actor><name>Al Pacino</name></actor></movie>",
    )
    write_image(env.files / "Set visit/pacino.jpg")
    note(env.files / "Set visit/Set visit.md", "---\npeople: al pacino\n---\n")
    env.scan()
    assert env.titles(movies.Person) == ["Al Pacino"]  # one person, keyed alike
    pacino = item(env, "Al Pacino")
    with env.reader.connect() as conn:
        held = list(
            conn.scalars(select(EntityContains.child_id).where(EntityContains.parent_id == pacino))
        )
    assert held == [item(env, "Set visit")]
