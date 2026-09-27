"""Tests for rebuilding and verifying the closure, and the repair action."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, delete, insert, select, update

from tagalot.core import closure
from tagalot.core.db import create_keep_engine
from tagalot.core.fts import index_is_consistent, sync_entities
from tagalot.core.maintenance import repair_indexes
from tagalot.core.models import Base, Entity, EntityAncestor, EntityContains, entity_fts
from tests.core.test_closure import _check, _stored, reference_closure

EDGES = [(1, 2), (1, 3), (2, 4), (3, 4), (4, 5), (6, 7)]


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(Entity), [{"id": i, "type": "t", "title": f"E{i}"} for i in range(1, 9)]
        )
        closure.add_entities(conn, range(1, 9))
        closure.apply(conn, added=EDGES)
        sync_entities(conn, range(1, 9))
    yield engine
    engine.dispose()


def test_rebuild_all_matches_the_reference(engine: Engine) -> None:
    with engine.begin() as conn:
        count = closure.rebuild_all(conn)
        rows = _check(conn)
    assert count == len(rows) == len(reference_closure(range(1, 9), EDGES))


def test_verify_reports_nothing_when_consistent(engine: Engine) -> None:
    with engine.connect() as conn:
        assert closure.verify(conn).ok


def _corrupt(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(delete(EntityAncestor).where(EntityAncestor.entity_id == 5))
        conn.execute(insert(EntityAncestor).values(entity_id=7, ancestor_id=1, depth=1))
        conn.execute(
            update(EntityAncestor)
            .where(EntityAncestor.entity_id == 4, EntityAncestor.ancestor_id == 1)
            .values(depth=9)
        )


def test_verify_finds_missing_extra_and_wrong_depths(engine: Engine) -> None:
    _corrupt(engine)
    with engine.connect() as conn:
        before = _stored(conn)
        check = closure.verify(conn)
        assert _stored(conn) == before  # verify never changes anything
    assert not check.ok
    assert (5, 1, 3) in check.missing
    assert (5, 5, 0) in check.missing
    assert (7, 1, 1) in check.extra
    assert (4, 1, 2) in check.missing  # the correct depth...
    assert (4, 1, 9) in check.extra  # ...and the wrong one


def test_rebuild_all_repairs_corruption(engine: Engine) -> None:
    _corrupt(engine)
    with engine.begin() as conn:
        closure.rebuild_all(conn)
        _check(conn)
        assert closure.verify(conn).ok


def test_rebuild_terminates_on_a_corrupt_cycle(engine: Engine) -> None:
    # A cycle can only come from outside apply(); the depth cap still ends the rebuild.
    with engine.begin() as conn:
        conn.execute(insert(EntityContains).values(parent_id=5, child_id=1))
        closure.rebuild_all(conn)
        rows = _stored(conn)
    assert rows[(1, 5)] == 1
    assert rows[(5, 1)] == 3


def test_repair_indexes_reports_and_fixes(engine: Engine) -> None:
    with engine.begin() as conn:
        clean = repair_indexes(conn)
    assert not clean.found_problems

    _corrupt(engine)
    with engine.begin() as conn:
        conn.execute(entity_fts.delete().where(entity_fts.c.rowid == 3))
        report = repair_indexes(conn)
        assert closure.verify(conn).ok
        assert index_is_consistent(conn)
    assert report.found_problems
    assert (report.closure_was_ok, report.search_index_was_ok) == (False, False)
    assert report.search_rows == 8
    with engine.connect() as conn:
        assert report.closure_rows == len(_stored(conn))


def test_rebuild_of_an_empty_keep(tmp_path: Path) -> None:
    engine = create_keep_engine(tmp_path / "empty.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        assert closure.rebuild_all(conn) == 0
        assert closure.verify(conn).ok
        assert list(conn.execute(select(EntityAncestor.entity_id))) == []
    engine.dispose()
