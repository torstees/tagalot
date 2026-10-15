"""Any item's own picture (#439): chosen by the user, named in its note, beside its note in
the notes folder, then the theme's chain, then its gallery's first picture."""

from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.item_pictures import (
    NOTE,
    PICTURE_ROLE,
    USER,
    PictureError,
    own_picture,
    restore_picture,
    set_picture,
)
from tagalot.core.models import Resource
from tagalot.core.picture_problems import NOT_FOUND, NOT_THERE, picture_problems
from tagalot.core.thumbnails.cache import ThumbCache
from tagalot.core.thumbnails.resolve import ThumbnailResolver
from tests.core.media_files import png_bytes
from tests.core.test_notes_folder import Cast, Env, env

__all__ = ["env"]  # fixtures

PACINO = "---\ntitle: Al Pacino\n{key}---\nAn actor.\n"


def _put(env: Env, *relpaths: str) -> None:
    for relpath in relpaths:
        path = env.files / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes((12, 8)))


def _resource(env: Env, relpath: str) -> int:
    with env.reader.connect() as conn:
        found = conn.scalar(select(Resource.id).where(Resource.relpath == relpath))
    assert found is not None, relpath
    return found


def _own(env: Env, title: str = "Al Pacino") -> tuple[str, str] | None:
    with env.reader.connect() as conn:
        found = own_picture(conn, env.item(title)["id"])
    return None if found is None else (found.relpath, found.source)


def _thumbnail_file(env: Env, tmp_path: Path, title: str = "Al Pacino") -> str | None:
    cache = ThumbCache(tmp_path / "thumbs.db")
    try:
        resolver = ThumbnailResolver(
            env.reader, env.writer, Cast, cache, lambda root: str(env.files), 32
        )
        result = resolver.resolve(env.item(title)["id"])
    finally:
        cache.close()
    if result.resource_id is None:
        return None
    with env.reader.connect() as conn:
        return conn.scalar(select(Resource.relpath).where(Resource.id == result.resource_id))


def test_the_picture_its_note_names(env: Env, tmp_path: Path) -> None:
    _put(env, "Notes/Actors/pics/pacino.png", "Notes/Actors/other.png")
    env.write("Notes/Actors/Al Pacino.md", PACINO.format(key="cover: pics/pacino.png\n"))
    env.scan()
    assert _own(env) == ("Notes/Actors/pics/pacino.png", NOTE)
    assert "cover" not in env.item("Al Pacino")["extra"]  # a picture, not an Extra field
    assert _thumbnail_file(env, tmp_path) == "Notes/Actors/pics/pacino.png"

    # `image` too; `cover` wins when both are given, whatever their order
    key = "image: other.png\ncover: /Notes/Actors/pics/pacino.png\n"  # from the top
    env.write("Notes/Actors/Al Pacino.md", PACINO.format(key=key))
    env.scan()
    assert _own(env) == ("Notes/Actors/pics/pacino.png", NOTE)
    env.write("Notes/Actors/Al Pacino.md", PACINO.format(key="image: '[[other.png]]'\n"))
    env.scan()
    assert _own(env) == ("Notes/Actors/other.png", NOTE)


def test_a_picture_beside_its_note(env: Env, tmp_path: Path) -> None:
    _put(env, "Notes/Actors/Al Pacino.png", "Notes/Actors/Al Pacino/photo.png")
    env.write("Notes/Actors/Al Pacino.md", PACINO.format(key=""))
    env.scan()
    assert _own(env) == ("Notes/Actors/Al Pacino.png", NOTE)
    (env.files / "Notes/Actors/Al Pacino.png").unlink()
    env.scan()  # gone: its gallery's first picture is used (no picture from the chain)
    assert _own(env) is None
    assert _thumbnail_file(env, tmp_path) == "Notes/Actors/Al Pacino/photo.png"


def test_a_named_picture_not_found_is_listed_until_it_is(env: Env) -> None:
    env.write("Notes/Actors/Al Pacino.md", PACINO.format(key="cover: pacino.png\n"))
    env.scan()
    assert _own(env) is None
    with env.reader.connect() as conn:
        [problem] = picture_problems(conn, lambda root: str(env.files))
    assert (problem.kind, problem.title, problem.role) == (NOT_FOUND, "Al Pacino", PICTURE_ROLE)
    assert [(t.relpath, t.state) for t in problem.tried] == [("Notes/Actors/pacino.png", NOT_THERE)]
    _put(env, "Notes/Actors/pacino.png")
    env.scan()  # the note didn't change; the picture came
    assert _own(env) == ("Notes/Actors/pacino.png", NOTE)
    with env.reader.connect() as conn:
        assert picture_problems(conn, lambda root: str(env.files)) == []

    env.write("Notes/Actors/Al Pacino.md", PACINO.format(key="cover: https://x.org/a.png\n"))
    env.scan()
    with env.reader.connect() as conn:
        [problem] = picture_problems(conn, lambda root: None)
    assert problem.tried == ()
    assert problem.reason is not None
    assert "web address" in problem.reason


def test_the_users_choice_wins_and_undoes(env: Env) -> None:
    _put(env, "Notes/Actors/Al Pacino.png", "Aurora/still.png")
    env.write("Notes/Actors/Al Pacino.md", PACINO.format(key=""))
    env.scan()
    pacino, still = env.item("Al Pacino")["id"], _resource(env, "Aurora/still.png")
    change = env.writer.run(lambda conn: set_picture(conn, pacino, still, "Set"))
    assert _own(env) == ("Aurora/still.png", USER)
    env.scan()  # the note's picture stays the note's; the user's is used
    assert _own(env) == ("Aurora/still.png", USER)
    forget = env.writer.run(lambda conn: set_picture(conn, pacino, None, "Usual"))
    assert _own(env) == ("Notes/Actors/Al Pacino.png", NOTE)  # at once, no scan
    env.writer.run(lambda conn: restore_picture(conn, forget, forward=False))
    assert _own(env) == ("Aurora/still.png", USER)
    env.writer.run(lambda conn: restore_picture(conn, change, forward=False))
    assert _own(env) == ("Notes/Actors/Al Pacino.png", NOTE)

    with env.reader.connect() as conn:
        note = conn.scalar(select(Resource.id).where(Resource.relpath.like("%Al Pacino.md")))
    assert note is not None
    with pytest.raises(PictureError):
        env.writer.run(lambda conn: set_picture(conn, pacino, note, "Set"))
