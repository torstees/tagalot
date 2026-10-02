"""Items side by side for the compare pane (#119)."""

from sqlalchemy import Connection, select

from tagalot.core.compare import MAX_FILES, MAX_ITEMS, Comparison, compare_items
from tagalot.core.detail import PATH_MARK
from tagalot.core.models import Entity
from tagalot.core.tags import add_tag_path, tag_entities
from tests.core.media_files import write_mp3
from tests.themes.test_music import Env, env, library

__all__ = ["env", "library"]  # fixtures


def _id(conn: Connection, type_id: str, title: str) -> int:
    found = conn.scalars(
        select(Entity.id).where(Entity.type == type_id, Entity.title == title).order_by(Entity.id)
    ).first()
    assert found is not None, title
    return found


def _compare(env: Env, ids: list[int]) -> Comparison:
    with env.reader.connect() as conn:
        return compare_items(conn, env.schema, ids, lambda _: str(env.files))


def test_two_songs_side_by_side(env: Env) -> None:
    env.scan()
    with env.reader.connect() as conn:
        so_what = _id(conn, "music.song", "So What")
        freddie = _id(conn, "music.song", "Freddie Freeloader")

    def tag(conn: Connection) -> None:
        tag_entities(conn, [so_what], [add_tag_path(conn, ["Mood", "Cool"])])

    env.writer.run(tag)
    found = _compare(env, [so_what, freddie])
    assert [i.title for i in found.items] == ["So What", "Freddie Freeloader"]
    assert found.more == 0
    keys = [r.key for r in found.rows]
    assert keys[:2] == ["type", "title"]
    assert keys[-3:] == ["format", "size", "files"]
    assert "field:album" in keys
    album = found.row("field:album")
    assert album is not None
    assert album.values == ("Kind of Blue", "Kind of Blue")
    assert not album.differs
    track = found.row("field:track")
    assert track is not None
    assert track.values == ("1", "2")
    assert track.differs
    assert found.row("tags") is not None
    assert found.row("tags").values == ("Cool", "")  # type: ignore[union-attr]
    # So What is one song in two files (a FLAC and an MP3: versions)
    formats = found.row("format")
    assert formats is not None
    assert formats.values == ("FLAC, MP3", "MP3")
    files = found.row("files")
    assert files is not None
    assert files.values[0].count("\n") == 1
    assert "Kind of Blue/01 So What.flac" in files.values[0]
    assert found.row("contents") is None  # songs contain nothing


def test_containers_show_how_much_they_hold(env: Env) -> None:
    env.scan()
    with env.reader.connect() as conn:
        blue = _id(conn, "music.album", "Kind of Blue")
        hits = _id(conn, "music.album", "Jazz Hits")
    contents = _compare(env, [blue, hits]).row("contents")
    assert contents is not None
    assert contents.values == ("2 items", "2 items")


def test_duplicates_gone_items_and_the_limit(env: Env) -> None:
    env.scan()
    with env.reader.connect() as conn:
        songs = list(conn.scalars(select(Entity.id).where(Entity.type == "music.song")))
    assert len(songs) > MAX_ITEMS
    found = _compare(env, [songs[0], songs[0], 999_999, *songs[1:]])
    assert [i.id for i in found.items] == songs[:MAX_ITEMS]
    assert found.more == len(songs) - MAX_ITEMS
    assert all(len(r.values) == MAX_ITEMS for r in found.rows)


def test_many_files_are_cut_short(env: Env) -> None:
    folder = env.files / "Many"
    folder.mkdir()
    for n in range(MAX_FILES + 2):  # one song, in many files: versions
        write_mp3(folder / f"echo {n}.mp3", tags={"title": "Echo", "album": "Many"})
    env.scan()
    with env.reader.connect() as conn:
        song = _id(conn, "music.song", "Echo")
        album = _id(conn, "music.album", "Many")
    found = _compare(env, [song, album])
    files = found.row("files")
    assert files is not None
    lines = files.values[0].split("\n")
    assert len(lines) == MAX_FILES + 1
    assert lines[-1] == "and 2 more"
    assert files.values[1] == f"Music {PATH_MARK} Many"  # an album's folder: where it is
    assert found.row("format").values == ("MP3", "")  # type: ignore[union-attr]
