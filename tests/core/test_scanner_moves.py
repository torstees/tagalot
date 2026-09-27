"""Tests for move detection: missing resources reattached to new paths by fingerprint."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, insert, select, update

from tagalot.core.db import create_keep_engine
from tagalot.core.fingerprint import compute_fingerprints, pending_fingerprints, store_fingerprints
from tagalot.core.keep import RootConfig
from tagalot.core.models import (
    Base,
    Entity,
    EntityResource,
    EntityTag,
    Resource,
    ResourceKind,
    ResourceStatus,
    Tag,
)
from tagalot.core.roots import local_path, sync_roots
from tagalot.core.scanner import apply_diff, detect_moves, diff_root, load_known, walk_root

OK, MISSING = ResourceStatus.OK, ResourceStatus.MISSING
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        sync_roots(conn, [RootConfig("r", "R", str(tmp_path / "root")), RootConfig("s", "S", "/s")])
    yield engine
    engine.dispose()


def _res(
    engine: Engine,
    relpath: str,
    fp: bytes | None,
    status: ResourceStatus = OK,
    root_id: str = "r",
    first_seen: datetime = T0,
) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                insert(Resource)
                .values(
                    root_id=root_id,
                    relpath=relpath,
                    kind=ResourceKind.FILE,
                    size=1,
                    mtime_ns=1,
                    fingerprint=fp,
                    status=status,
                    first_seen_at=first_seen,
                )
                .returning(Resource.id)
            ).scalar_one()
        )


def _link(engine: Engine, entity_id: int, resource_id: int, role: str = "audio") -> None:
    with engine.begin() as conn:
        exists = conn.scalar(select(func.count()).where(Entity.id == entity_id))
        if not exists:
            conn.execute(insert(Entity).values(id=entity_id, type="generic.file", title="E"))
        conn.execute(
            insert(EntityResource).values(entity_id=entity_id, resource_id=resource_id, role=role)
        )


def _detect(engine: Engine, candidates: list[int] | None = None) -> list[tuple[int, int]]:
    with engine.begin() as conn:
        return [(m.old_id, m.new_id) for m in detect_moves(conn, candidates)]


def _ids(engine: Engine) -> set[int]:
    with engine.connect() as conn:
        return set(conn.scalars(select(Resource.id)))


def test_move_transfers_links_thumbnail_members_and_history(engine: Engine) -> None:
    old = _res(engine, "Old/song.flac", b"A", MISSING, first_seen=T0)
    new = _res(engine, "New/song.flac", b"A", first_seen=T0 + timedelta(days=30))
    member = _res(engine, "Old/song.flac#inner", None)
    _link(engine, 1, old, "audio")
    _link(engine, 2, old, "cover")
    with engine.begin() as conn:
        conn.execute(update(Entity).values(thumb_resource_id=old))
        conn.execute(update(Resource).where(Resource.id == member).values(parent_resource_id=old))

    assert _detect(engine) == [(old, new)]
    with engine.connect() as conn:
        links = set(
            conn.execute(
                select(EntityResource.entity_id, EntityResource.resource_id, EntityResource.role)
            )
        )
        assert links == {(1, new, "audio"), (2, new, "cover")}
        assert set(conn.scalars(select(Entity.thumb_resource_id))) == {new}
        assert conn.scalar(select(Resource.parent_resource_id).where(Resource.id == member)) == new
        assert conn.scalar(select(Resource.first_seen_at).where(Resource.id == new)) == T0
    assert old not in _ids(engine)


def test_move_across_roots(engine: Engine) -> None:
    old = _res(engine, "a.flac", b"A", MISSING, root_id="s")
    new = _res(engine, "moved/a.flac", b"A", root_id="r")
    _link(engine, 1, old)
    assert _detect(engine) == [(old, new)]


def test_rename_is_a_move(engine: Engine) -> None:
    old = _res(engine, "Album/01 track.flac", b"A", MISSING)
    new = _res(engine, "Album/01 - Intro.flac", b"A")
    assert _detect(engine) == [(old, new)]


def test_no_match_changes_nothing(engine: Engine) -> None:
    _res(engine, "a.flac", b"A", MISSING)
    _res(engine, "b.flac", b"B")
    _res(engine, "c.flac", None)  # not fingerprinted yet
    before = _ids(engine)
    assert _detect(engine) == []
    assert _ids(engine) == before


def test_missing_file_without_fingerprint_cannot_be_matched(engine: Engine) -> None:
    _res(engine, "a.flac", None, MISSING)
    _res(engine, "b.flac", b"A")
    assert _detect(engine) == []


def test_linked_copy_is_not_a_move(engine: Engine) -> None:
    old = _res(engine, "a.flac", b"A", MISSING)
    copy = _res(engine, "copy/a.flac", b"A")
    _link(engine, 1, old)
    _link(engine, 2, copy)
    assert _detect(engine) == []
    assert old in _ids(engine)


def test_candidates_limit_the_search(engine: Engine) -> None:
    old = _res(engine, "a.flac", b"A", MISSING)
    new = _res(engine, "b.flac", b"A")
    assert _detect(engine, candidates=[]) == []
    assert _detect(engine, candidates=[new + 100]) == []
    assert _detect(engine, candidates=[new]) == [(old, new)]


def test_ambiguous_moves_pair_only_by_unique_name(engine: Engine) -> None:
    # Two albums with an identical cover.jpg were both renamed; plus two identical
    # placeholders that moved and were renamed in ways the name can't resolve.
    old_a = _res(engine, "Album A/cover.jpg", b"C", MISSING)
    old_b = _res(engine, "Album B/front.jpg", b"C", MISSING)
    new_a = _res(engine, "A (2020)/cover.jpg", b"C")
    new_b = _res(engine, "B (2021)/front.jpg", b"C")
    _res(engine, "x/one.png", b"P", MISSING)
    _res(engine, "y/one.png", b"P", MISSING)
    _res(engine, "z/two.png", b"P")
    assert sorted(_detect(engine)) == sorted([(old_a, new_a), (old_b, new_b)])


def test_one_missing_two_new_copies_pairs_by_name(engine: Engine) -> None:
    old = _res(engine, "Album/cover.jpg", b"C", MISSING)
    same_name = _res(engine, "Album (Deluxe)/cover.jpg", b"C")
    _res(engine, "Album (Deluxe)/scan.jpg", b"C")
    assert _detect(engine) == [(old, same_name)]


def test_one_missing_two_new_different_names_is_left_alone(engine: Engine) -> None:
    old = _res(engine, "a.jpg", b"C", MISSING)
    _res(engine, "b.jpg", b"C")
    _res(engine, "c.jpg", b"C")
    assert _detect(engine) == []
    assert old in _ids(engine)


def test_running_twice_finds_nothing_new(engine: Engine) -> None:
    _res(engine, "a.flac", b"A", MISSING)
    _res(engine, "b.flac", b"A")
    assert len(_detect(engine)) == 1
    assert _detect(engine) == []


# --- end to end with the real scanner ---


def _full_scan(engine: Engine, root: Path, when: datetime) -> list[tuple[str, str]]:
    entries = list(walk_root(str(root)))
    with engine.begin() as conn:
        applied = apply_diff(conn, "r", diff_root(load_known(conn, "r"), entries), when)
        jobs = pending_fingerprints(conn, root_id="r")
    results = list(compute_fingerprints(jobs, lambda j: local_path(str(root), j.relpath)))
    with engine.begin() as conn:
        store_fingerprints(conn, results)
        moves = detect_moves(conn, applied.new_ids.values())
    return [(m.old_path, m.new_path) for m in moves]


def test_renamed_folder_keeps_its_tags(engine: Engine, tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "Old Name").mkdir(parents=True)
    (root / "Old Name/01.flac").write_bytes(os.urandom(200_000))
    (root / "Old Name/02.flac").write_bytes(os.urandom(200_000))
    assert _full_scan(engine, root, T0) == []

    with engine.begin() as conn:
        rid = conn.scalar(select(Resource.id).where(Resource.relpath == "Old Name/01.flac"))
        conn.execute(insert(Entity).values(id=1, type="generic.file", title="Song"))
        conn.execute(insert(EntityResource).values(entity_id=1, resource_id=rid, role="file"))
        conn.execute(insert(Tag).values(id=1, name="Favorites"))
        conn.execute(insert(EntityTag).values(entity_id=1, tag_id=1))

    (root / "Old Name").rename(root / "New Name")
    moves = _full_scan(engine, root, T0 + timedelta(days=1))
    assert sorted(moves) == [
        ("r:Old Name/01.flac", "r:New Name/01.flac"),
        ("r:Old Name/02.flac", "r:New Name/02.flac"),
    ]
    with engine.connect() as conn:
        linked = conn.scalar(
            select(Resource.relpath)
            .join(EntityResource, EntityResource.resource_id == Resource.id)
            .where(EntityResource.entity_id == 1)
        )
        assert linked == "New Name/01.flac"
        assert conn.scalar(select(func.count()).select_from(EntityTag)) == 1  # tag kept
        assert conn.scalar(select(func.count()).where(Resource.status == MISSING)) == 0
        assert conn.scalar(select(func.count()).select_from(Resource)) == 2
