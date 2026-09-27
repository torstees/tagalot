"""Randomized closure maintenance (issue #37).

Each run applies a seeded random sequence of operations: edge additions (including diamonds
and deliberate cycle attempts), removals, mixed batches, new entities, and detach-then-delete.
After every step the stored closure must equal a fresh computation (``verify``, the same
query ``rebuild_all`` uses) and a plain-Python reference, and the stored edges must equal a
Python model that applies the same cycle rule. A failure names its seed, so it replays.
"""

import random
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, delete, insert

from tagalot.core import closure
from tagalot.core.db import create_keep_engine
from tagalot.core.models import Base, Entity
from tests.core.test_closure import _check, _edges

SEEDS = range(30)
STEPS = 40
START_ENTITIES = 10

Edge = tuple[int, int]


class Model:
    """What the edges should be, applying the same rules as closure.apply()."""

    def __init__(self) -> None:
        self.entities: set[int] = set()
        self.edges: set[Edge] = set()

    def reaches(self, start: int, target: int) -> bool:
        """Whether ``target`` is ``start`` or one of its ancestors."""
        stack, seen = [start], set()
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node not in seen:
                seen.add(node)
                stack.extend(p for p, c in self.edges if c == node)
        return False

    def apply(self, added: list[Edge], removed: list[Edge]) -> tuple[list[Edge], list[Edge]]:
        """Returns (accepted additions, rejected additions)."""
        self.edges -= set(removed)
        accepted, rejected = [], []
        for edge in dict.fromkeys(added):
            p, c = edge
            if edge in self.edges:
                continue
            if self.reaches(p, c):  # p == c, or c is already an ancestor of p
                rejected.append(edge)
            else:
                self.edges.add(edge)
                accepted.append(edge)
        return accepted, rejected

    def detach(self, entity: int) -> None:
        self.edges = {(p, c) for p, c in self.edges if entity not in (p, c)}
        self.entities.discard(entity)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _new_entity(conn: Connection, model: Model) -> int:
    new_id = max(model.entities, default=0) + 1
    conn.execute(insert(Entity).values(id=new_id, type="t", title=f"E{new_id}"))
    model.entities.add(new_id)
    return new_id


def _random_edge(rng: random.Random, model: Model) -> Edge:
    nodes = sorted(model.entities)
    if rng.random() < 0.3 and model.edges:
        # Bias toward diamonds and cycles: reuse a node already in an edge.
        p, c = rng.choice(sorted(model.edges))
        return rng.choice([(c, p), (p, rng.choice(nodes)), (rng.choice(nodes), c)])
    return rng.choice(nodes), rng.choice(nodes)


@pytest.mark.parametrize("seed", SEEDS)
def test_random_sequences_match_a_full_rebuild(engine: Engine, seed: int) -> None:
    rng = random.Random(seed)
    model = Model()
    with engine.begin() as conn:
        for _ in range(START_ENTITIES):
            _new_entity(conn, model)
        closure.add_entities(conn, model.entities)

    for step in range(STEPS):
        context = f"seed {seed}, step {step}"
        with engine.begin() as conn:
            kind = rng.choices(["add", "remove", "mixed", "new", "delete"], [5, 3, 2, 1, 1])[0]
            added: list[Edge] = []
            removed: list[Edge] = []
            if kind == "new":
                new_id = _new_entity(conn, model)
                if rng.random() < 0.5:
                    closure.add_entities(conn, [new_id])
                added = [(rng.choice(sorted(model.entities)), new_id)]
            elif kind == "delete" and len(model.entities) > 3:
                victim = rng.choice(sorted(model.entities))
                closure.detach(conn, [victim])
                conn.execute(delete(Entity).where(Entity.id == victim))
                model.detach(victim)
            if kind in ("add", "mixed"):
                added += [_random_edge(rng, model) for _ in range(rng.randint(1, 4))]
            if kind in ("remove", "mixed") and model.edges:
                removed = rng.sample(
                    sorted(model.edges), k=min(len(model.edges), rng.randint(1, 3))
                )
                if rng.random() < 0.3:
                    removed.append((999, 998))  # an edge that doesn't exist

            result = closure.apply(conn, added=added, removed=removed)
            expected_added, expected_rejected = model.apply(added, removed)

            assert result.added == expected_added, context
            assert [e for e, _ in result.rejected] == expected_rejected, context
            assert _edges(conn) == model.edges, context
            _check(conn)  # equals the plain-Python reference closure
            check = closure.verify(conn)  # equals what rebuild_all() would produce
            assert check.ok, (context, sorted(check.missing), sorted(check.extra))


def test_runs_include_the_interesting_cases(engine: Engine) -> None:
    """Guard against the generator drifting into only trivial graphs."""
    diamonds = cycles = 0
    for seed in SEEDS:
        rng = random.Random(seed)
        model = Model()
        model.entities = set(range(1, START_ENTITIES + 1))
        for _ in range(STEPS):
            _, rejected = model.apply([_random_edge(rng, model) for _ in range(3)], [])
            cycles += len(rejected)
            children = [c for _, c in model.edges]
            diamonds += len(children) - len(set(children))
    assert cycles > 100
    assert diamonds > 100
