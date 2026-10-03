"""Not a duplicate: dismissing groups and pairs from Dedupe, and undo (#121); and which
comparisons can keep both items' files as versions."""

from sqlalchemy import Connection, select

from tagalot.core.compare import compare_items
from tagalot.core.dedupe import DuplicateFile, DuplicateGroup, NearPair
from tagalot.core.models import Entity, ResourceStatus
from tagalot.core.not_duplicates import (
    EXACT,
    SIMILAR,
    group_entry,
    load_dismissals,
    pair_entry,
    restore_not_duplicates,
    set_not_duplicate,
)
from tests.themes.test_music import Env, env, library

__all__ = ["env", "library"]  # fixtures


def _group(*resource_ids: int) -> DuplicateGroup:
    files = tuple(
        DuplicateFile(r, "r", "Music", f"{r}.mp3", 10, None, ResourceStatus.OK, None)
        for r in resource_ids
    )
    return DuplicateGroup(b"\x01\x02", 10, files)


def test_entries() -> None:
    assert group_entry(_group(7, 3)) == (EXACT, "0102", "3,7")
    pair = NearPair("music.song", (9, "A"), (4, "B"), 1.0)
    swapped = NearPair("music.song", (4, "B"), (9, "A"), 1.0)
    assert pair_entry(pair) == pair_entry(swapped) == (SIMILAR, "4:9", "")


def test_dismissing_restoring_and_undo(env: Env) -> None:
    group = group_entry(_group(3, 7))
    pair = pair_entry(NearPair("music.song", (9, "A"), (4, "B"), 1.0))

    def check(conn: Connection) -> None:
        change = set_not_duplicate(conn, [group, pair])
        assert change.label == "Mark 2 entries as not a duplicate"
        found = load_dismissals(conn)
        assert found.hides(group)
        assert found.hides(pair)
        # A third copy: the group isn't the one the user judged.
        assert not found.hides(group_entry(_group(3, 7, 8)))

        restore_not_duplicates(conn, change, forward=False)
        assert load_dismissals(conn).rows == frozenset()
        restore_not_duplicates(conn, change, forward=True)
        assert load_dismissals(conn).hides(pair)

        again = set_not_duplicate(conn, [pair], dismissed=False)
        assert again.label == "Show 1 pair as a duplicate again"
        assert load_dismissals(conn).rows == {group}
        restore_not_duplicates(conn, again, forward=False)
        assert load_dismissals(conn).hides(pair)

        one = set_not_duplicate(conn, [group_entry(_group(3, 7, 8))])
        assert one.label == "Mark 1 group as not a duplicate"
        assert load_dismissals(conn).hides(group_entry(_group(3, 7, 8)))  # re-marked

    env.writer.run(check)


def test_which_comparisons_keep_files_as_versions(env: Env) -> None:
    env.scan()
    with env.reader.connect() as conn:

        def ids(type_id: str) -> list[int]:
            return list(conn.scalars(select(Entity.id).where(Entity.type == type_id)))

        song_ids, album_ids = ids("music.song"), ids("music.album")
        assert compare_items(conn, env.schema, song_ids[:2], lambda _: None).versions
        # An album's main role is its one folder: merging can't keep both.
        assert not compare_items(conn, env.schema, album_ids[:2], lambda _: None).versions
        assert not compare_items(conn, env.schema, song_ids[:1], lambda _: None).versions
        mixed = [song_ids[0], album_ids[0]]
        assert not compare_items(conn, env.schema, mixed, lambda _: None).versions
