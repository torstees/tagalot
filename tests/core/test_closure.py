"""Tests for closure table maintenance (DESIGN.md §6 "Closure maintenance")."""

from collections import deque
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, delete, insert, select

from tagalot.core import closure
from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Entity, EntityAncestor, EntityContains

Rows = dict[tuple[int, int], int]


def reference_closure(entities: Iterable[int], edges: Iterable[tuple[int, int]]) -> Rows:
    """The closure computed in plain Python: BFS upward from each entity, minimum depths."""
    parents: dict[int, list[int]] = {}
    for p, c in edges:
        parents.setdefault(c, []).append(p)
    rows: Rows = {}
    for start in entities:
        seen = {start: 0}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for p in parents.get(node, []):
                if p not in seen:
                    seen[p] = seen[node] + 1
                    queue.append(p)
        rows.update({(start, a): d for a, d in seen.items()})
    return rows


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(Entity), [{"id": i, "type": "t", "title": f"E{i}"} for i in range(1, 13)]
        )
        closure.add_entities(conn, range(1, 13))
    yield engine
    engine.dispose()


def _stored(conn: Connection) -> Rows:
    rows = conn.execute(
        select(EntityAncestor.entity_id, EntityAncestor.ancestor_id, EntityAncestor.depth)
    )
    return {(e, a): d for e, a, d in rows}


def _edges(conn: Connection) -> set[tuple[int, int]]:
    return {
        (p, c) for p, c in conn.execute(select(EntityContains.parent_id, EntityContains.child_id))
    }


def _check(conn: Connection) -> Rows:
    """The stored closure must equal the reference computed from the stored edges."""
    entities = list(conn.scalars(select(Entity.id)))
    expected = reference_closure(entities, _edges(conn))
    assert _stored(conn) == expected
    return expected


def _apply(
    engine: Engine, added: Iterable[tuple[int, int]] = (), removed: Iterable[tuple[int, int]] = ()
) -> closure.ClosureResult:
    with engine.begin() as conn:
        result = closure.apply(conn, added, removed)
        _check(conn)
    return result


def test_self_rows_for_entities_without_edges(engine: Engine) -> None:
    with engine.connect() as conn:
        assert _check(conn) == {(i, i): 0 for i in range(1, 13)}


def test_chain(engine: Engine) -> None:
    _apply(engine, [(1, 2), (2, 3)])  # artist 1 > album 2 > song 3
    with engine.connect() as conn:
        rows = _check(conn)
    assert rows[(3, 1)] == 2
    assert rows[(3, 2)] == 1
    assert (1, 3) not in rows


def test_adding_above_an_existing_subtree_updates_every_descendant(engine: Engine) -> None:
    _apply(engine, [(2, 3), (3, 4), (3, 5)])
    _apply(engine, [(1, 2)])
    with engine.connect() as conn:
        rows = _check(conn)
    assert rows[(4, 1)] == rows[(5, 1)] == 3


def test_diamond_keeps_the_minimum_depth(engine: Engine) -> None:
    # 1 contains 2 and 3; both contain 4; and 1 contains 4 directly.
    _apply(engine, [(1, 2), (1, 3), (2, 4), (3, 4)])
    with engine.connect() as conn:
        assert _check(conn)[(4, 1)] == 2
    _apply(engine, [(1, 4)])
    with engine.connect() as conn:
        assert _check(conn)[(4, 1)] == 1
    _apply(engine, removed=[(1, 4)])  # depth grows back to 2: the table alone can't know
    with engine.connect() as conn:
        assert _check(conn)[(4, 1)] == 2


def test_removing_one_path_of_a_diamond_keeps_the_ancestor(engine: Engine) -> None:
    # A song on a studio album and a compilation by the same artist.
    _apply(engine, [(1, 2), (1, 3), (2, 4), (3, 4)])
    _apply(engine, removed=[(2, 4)])
    with engine.connect() as conn:
        rows = _check(conn)
    assert rows[(4, 1)] == 2  # still under the artist, via the compilation
    assert (4, 2) not in rows


def test_removing_the_only_path_drops_the_ancestor(engine: Engine) -> None:
    _apply(engine, [(1, 2), (2, 3), (3, 4)])
    _apply(engine, removed=[(1, 2)])
    with engine.connect() as conn:
        rows = _check(conn)
    assert not any(a == 1 for (e, a) in rows if e != 1)


def test_reparenting_in_one_batch(engine: Engine) -> None:
    _apply(engine, [(1, 3), (3, 4)])
    result = _apply(engine, added=[(2, 3)], removed=[(1, 3)])
    assert (result.added, result.removed) == ([(2, 3)], [(1, 3)])
    with engine.connect() as conn:
        rows = _check(conn)
    assert rows[(4, 2)] == 2
    assert (4, 1) not in rows


@pytest.mark.parametrize(
    ("setup", "edge"),
    [
        ([], (5, 5)),  # self loop
        ([(1, 2)], (2, 1)),  # two-cycle
        ([(1, 2), (2, 3), (3, 4)], (4, 1)),  # longer cycle
        ([(1, 2), (1, 3), (2, 4), (3, 4)], (4, 1)),  # through a diamond
    ],
)
def test_cycles_are_rejected(
    engine: Engine, setup: list[tuple[int, int]], edge: tuple[int, int]
) -> None:
    _apply(engine, setup)
    result = _apply(engine, [edge])
    assert result.added == []
    assert [e for e, _ in result.rejected] == [edge]
    with engine.connect() as conn:
        assert edge not in _edges(conn)


def test_cycles_within_one_batch_are_checked_in_order(engine: Engine) -> None:
    result = _apply(engine, [(1, 2), (2, 3), (3, 1), (3, 4)])
    assert result.added == [(1, 2), (2, 3), (3, 4)]
    assert [e for e, _ in result.rejected] == [(3, 1)]
    assert "cycle" in result.rejected[0][1]


def test_duplicates_and_missing_edges_are_skipped(engine: Engine) -> None:
    _apply(engine, [(1, 2)])
    result = _apply(engine, added=[(1, 2), (1, 2)], removed=[(7, 8)])
    assert (result.added, result.removed, result.rejected) == ([], [], [])


def test_new_entity_attached_in_the_same_batch(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(insert(Entity).values(id=50, type="t", title="new song"))
        closure.apply(conn, added=[(1, 50)])  # no add_entities call: the rebuild adds self rows
        rows = _check(conn)
    assert rows[(50, 50)] == 0
    assert rows[(50, 1)] == 1


def test_detach_before_deleting(engine: Engine) -> None:
    _apply(engine, [(1, 2), (2, 3), (3, 4)])
    with engine.begin() as conn:
        result = closure.detach(conn, [2])
        conn.execute(delete(Entity).where(Entity.id == 2))
        rows = _check(conn)
    assert set(result.removed) == {(1, 2), (2, 3)}
    assert (3, 1) not in rows  # without detach, (3, 1) would have survived the cascade
    assert rows[(4, 3)] == 1


def test_deleting_without_detach_leaves_stale_rows(engine: Engine) -> None:
    # Documents why detach exists.
    _apply(engine, [(1, 2), (2, 3)])
    with engine.begin() as conn:
        conn.execute(delete(Entity).where(Entity.id == 2))
        assert (3, 1) in _stored(conn)


def test_large_batch(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(Entity), [{"id": i, "type": "t", "title": f"S{i}"} for i in range(100, 2_100)]
        )
        # 20 albums under artist 1, 100 songs each.
        albums = list(range(100, 120))
        edges = [(1, a) for a in albums]
        edges += [(albums[(s - 120) % 20], s) for s in range(120, 2_100)]
        result = closure.apply(conn, added=edges)
        assert len(result.added) == len(edges)
        _check(conn)


def test_repeated_calls_in_one_transaction(engine: Engine) -> None:
    # The temporary work table must be reusable on the same connection.
    with engine.begin() as conn:
        for edge in [(1, 2), (2, 3), (3, 4)]:
            closure.apply(conn, added=[edge])
        closure.apply(conn, removed=[(2, 3)])
        closure.detach(conn, [4])
        rows = _check(conn)
    assert (4, 3) not in rows
    assert rows[(2, 1)] == 1
