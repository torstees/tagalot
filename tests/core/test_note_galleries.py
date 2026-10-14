"""Galleries from the notes folder (#388): a folder or archive named like a notes-folder
note, beside it, fills the matched item's first gallery role, and follows the note."""

import os

from sqlalchemy import select

from tagalot.core.models import EntityResource, Resource
from tagalot.core.triage import unlinked_files
from tests.core.media_files import png_bytes
from tests.core.test_notes_folder import PACINO, Cast, Env, env

__all__ = ["env"]  # fixtures

PNG = png_bytes((8, 8))
EMPTY_ZIP = b"PK\x05\x06" + b"\x00" * 18


def _put(env: Env, *relpaths: str) -> None:
    for relpath in relpaths:
        path = env.files / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(EMPTY_ZIP if relpath.endswith(".zip") else PNG)


def _gallery(env: Env, title: str, role: str = "photos") -> dict[str, bool]:
    """The item's role files: relpath -> made from folders."""
    with env.reader.connect() as conn:
        rows = conn.execute(
            select(Resource.relpath, EntityResource.by_folder)
            .join(Resource, Resource.id == EntityResource.resource_id)
            .where(EntityResource.entity_id == env.item(title)["id"], EntityResource.role == role)
        ).all()
    return {relpath: filled for relpath, filled in rows}


def test_a_folder_and_an_archive_named_like_the_note_fill_its_items_gallery(env: Env) -> None:
    env.write("Notes/Actors/Al Pacino.md", PACINO)
    _put(
        env,
        "Notes/Actors/Al Pacino/a.png",
        "Notes/Actors/Al Pacino/On set/b.png",  # at any depth
        "Notes/Actors/Al Pacino/More.zip",  # an archive inside
        "Notes/Actors/Al Pacino.zip",  # an archive beside the note
        "Notes/Actors/Robert De Niro/c.png",  # another name: no note matched it
        "Notes/Actors/x.png",  # loose in the notes folder
    )
    env.scan()
    assert not [p for p in Cast.seen if p.startswith("Notes")]  # still nothing for the theme
    assert _gallery(env, "Al Pacino") == {
        "Notes/Actors/Al Pacino/a.png": True,
        "Notes/Actors/Al Pacino/On set/b.png": True,
        "Notes/Actors/Al Pacino/More.zip": True,
        "Notes/Actors/Al Pacino.zip": True,
    }
    assert _gallery(env, "Robert De Niro") == {}


def test_the_gallery_follows_the_note(env: Env) -> None:
    env.write("Notes/Actors/Al Pacino.md", PACINO)
    _put(env, "Notes/Actors/Al Pacino/a.png")
    env.scan()
    assert list(_gallery(env, "Al Pacino")) == ["Notes/Actors/Al Pacino/a.png"]
    env.write("Notes/Actors/Al Pacino.md", "---\ntitle: Nobody Here\n---\n")
    env.scan()  # it stopped matching: its gallery goes with it
    assert _gallery(env, "Al Pacino") == {}
    env.write("Notes/Actors/Al Pacino.md", PACINO + "\nMore.\n")
    _put(env, "Notes/Actors/Al Pacino/b.png")
    env.scan()
    assert set(_gallery(env, "Al Pacino")) == {
        "Notes/Actors/Al Pacino/a.png",
        "Notes/Actors/Al Pacino/b.png",
    }
    os.remove(env.files / "Notes/Actors/Al Pacino/a.png")
    env.scan()
    assert list(_gallery(env, "Al Pacino")) == ["Notes/Actors/Al Pacino/b.png"]
    os.remove(env.files / "Notes/Actors/Al Pacino.md")
    env.scan()  # the note is gone
    assert _gallery(env, "Al Pacino") == {}


def test_a_notes_folder_inside_a_named_folder_keeps_its_own(env: Env) -> None:
    """The notes folder's pictures are its notes' galleries', never a named folder's."""
    env.notes = "Aurora/Stills/Notes"
    env.write("Aurora/Stills/Notes/Al Pacino.md", PACINO)
    _put(env, "Aurora/Stills/a.png", "Aurora/Stills/Notes/Al Pacino/b.png")
    env.scan()
    assert _gallery(env, "Aurora", "stills") == {"Aurora/Stills/a.png": True}
    assert _gallery(env, "Al Pacino") == {"Aurora/Stills/Notes/Al Pacino/b.png": True}
    env.scan()  # a second scan of the root leaves the note's gallery alone
    assert _gallery(env, "Al Pacino") == {"Aurora/Stills/Notes/Al Pacino/b.png": True}


def test_the_notes_folder_isnt_listed_as_unlinked(env: Env) -> None:
    env.write("Notes/Actors/Al Pacino.md", PACINO)
    _put(env, "Notes/Actors/Nobody/a.png", "Aurora/loose.png")
    env.scan()
    with env.reader.connect() as conn:
        everywhere = [f.relpath for f in unlinked_files(conn)]
        content = [f.relpath for f in unlinked_files(conn, notes=("r", "notes"))]  # case aside
    assert "Notes/Actors/Nobody/a.png" in everywhere
    assert content == ["Aurora/loose.png"]
