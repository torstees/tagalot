"""Triage lists: unlinked files, untagged items, items whose files are all missing (#110)."""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert, select, update

from tagalot.core.actions import delete_items, restore_action
from tagalot.core.models import Entity, EntityTag, Resource, ResourceStatus
from tagalot.core.scanner import compile_excludes
from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec, SearchSpecError
from tagalot.core.tags import TagTreeCache, add_tag
from tagalot.core.triage import (
    MISSING,
    UNLINKED,
    UNTAGGED,
    count_unlinked,
    dismiss,
    exact_pattern,
    restore_dismissals,
    unlinked_files,
)
from tests.core.media_files import write_image
from tests.themes.test_music import T0, Env, env, library

__all__ = ["env", "library"]  # fixtures

COVER = "Miles Davis/Kind of Blue/cover.jpg"


def _unlinked(env: Env) -> list[str]:
    with env.reader.connect() as conn:
        return [f.relpath for f in unlinked_files(conn)]


def _listed(env: Env, name: str, inherit: bool = False) -> list[str]:
    tree = TagTreeCache(env.reader).get()
    with env.reader.connect() as conn:
        hits = run_search(conn, SearchSpec(triage=name, inherit_tags=inherit), tree, limit=None)
    return sorted(h.title for h in hits)


def _set_status(env: Env, relpath_end: str, status: ResourceStatus) -> None:
    env.writer.run(
        lambda conn: conn.execute(
            update(Resource).where(Resource.relpath.endswith(relpath_end)).values(status=status)
        )
    )


def test_unlinked_files(env: Env) -> None:
    env.scan()
    assert _unlinked(env) == [COVER]  # folders aren't listed; every audio file is a song
    with env.reader.connect() as conn:
        assert count_unlinked(conn) == 1
    _set_status(env, "cover.jpg", ResourceStatus.MISSING)
    assert _unlinked(env) == []  # missing files aren't listed


def test_dismissing_a_file_until_it_changes(env: Env) -> None:
    env.scan()
    cover = env.writer.run(
        lambda conn: conn.scalar(select(Resource.id).where(Resource.relpath == COVER))
    )
    assert cover is not None
    change = env.writer.run(lambda conn: dismiss(conn, UNLINKED, [cover]))
    assert change.label == "Dismiss 1 file"
    assert _unlinked(env) == []
    env.writer.run(lambda conn: restore_dismissals(conn, change, forward=False))  # undo
    assert _unlinked(env) == [COVER]
    env.writer.run(lambda conn: restore_dismissals(conn, change, forward=True))  # redo
    assert _unlinked(env) == []

    write_image(env.files / COVER, (90, 90), "green")  # changed: back on the list
    env.scan(T0 + timedelta(hours=1))
    assert _unlinked(env) == [COVER]


def test_untagged_items(env: Env) -> None:
    env.scan()
    everything = _listed(env, UNTAGGED)
    assert "Kind of Blue" in everything
    assert "So What" in everything
    tag = env.writer.run(lambda conn: add_tag(conn, None, "Jazz"))
    env.writer.run(
        lambda conn: conn.execute(
            insert(EntityTag).values(entity_id=env.entity("Kind of Blue"), tag_id=tag)
        )
    )
    assert "Kind of Blue" not in _listed(env, UNTAGGED)
    assert "So What" in _listed(env, UNTAGGED)  # its own tags only
    assert "So What" not in _listed(env, UNTAGGED, inherit=True)  # its album's count


def test_dismissing_an_item_until_it_changes(env: Env) -> None:
    env.scan()
    so_what = env.entity("So What")
    env.writer.run(lambda conn: dismiss(conn, UNTAGGED, [so_what]))
    assert "So What" not in _listed(env, UNTAGGED)
    env.writer.run(  # an edit changes updated_at
        lambda conn: conn.execute(
            update(Entity).where(Entity.id == so_what).values(title="So What (live)")
        )
    )
    assert "So What (live)" in _listed(env, UNTAGGED)


def test_items_whose_files_are_all_missing(env: Env) -> None:
    env.scan()
    assert _listed(env, MISSING) == []
    _set_status(env, "01 So What.mp3", ResourceStatus.MISSING)
    assert _listed(env, MISSING) == []  # its FLAC is still there
    _set_status(env, "01 So What.flac", ResourceStatus.MISSING)
    assert _listed(env, MISSING) == ["So What"]
    _set_status(env, "loose.mp3", ResourceStatus.OFFLINE)
    assert _listed(env, MISSING) == ["So What"]  # offline isn't missing


def test_deleting_items_is_undoable(env: Env) -> None:
    env.scan()
    so_what = env.entity("So What")
    change = env.writer.run(lambda conn: delete_items(conn, env.schema, [so_what]))
    assert change.label == "Delete 1 item"
    with env.reader.connect() as conn:
        assert conn.scalar(select(Entity.id).where(Entity.id == so_what)) is None
    env.writer.run(lambda conn: restore_action(conn, env.schema, change, forward=False))
    assert env.entity("So What") == so_what
    assert env.contents()["Kind of Blue"] == ["Freddie Freeloader", "So What"]


def test_triage_in_search_specs() -> None:
    spec = SearchSpec(triage=MISSING)
    assert SearchSpec.from_json(spec.to_json()) == spec
    with pytest.raises(SearchSpecError, match="triage must be"):
        SearchSpec(triage="nope")


@pytest.mark.parametrize("relpath", ["a/b.png", "odd [1]/x*?.png", "**/x"])
def test_exact_exclude_patterns(relpath: str) -> None:
    pattern = compile_excludes([exact_pattern(relpath)])
    assert pattern is not None
    assert pattern.match(relpath)
    assert not pattern.match(relpath + "x")
    assert not pattern.match("other/" + relpath)
    assert Path(relpath).name  # a file name, as the triage list gives
