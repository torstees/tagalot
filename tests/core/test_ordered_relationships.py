"""Ordered relationships (#317): a paper's authors in order, from files and by hand, kept
through scans, undo, and merges, and listed in order on pages and in searches."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, inspect, select

from tagalot.core.db import open_keep_database
from tagalot.core.detail import load_detail
from tagalot.core.entity_state import restore_states
from tagalot.core.ingest import IngestError, IngestSession
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.merge import merge_items
from tagalot.core.models import UserOrder
from tagalot.core.relations import RelationError, add_related, move_related, remove_related
from tagalot.core.search import POSITION, SearchError, run_search
from tagalot.core.search_fields import search_fields
from tagalot.core.search_spec import SearchSpec, SortKey
from tagalot.core.tags import TagTree
from tagalot.core.theme_db import open_theme, sync_theme_schema
from tagalot.core.theme_schema import ThemeSchema, build_theme_schema
from tagalot.themes.api import (
    DetailView,
    Entity,
    EntityRef,
    Section,
    Theme,
    ThemeDeclarationError,
    related,
)
from tagalot.themes.loader import validate_theme


class Person(Entity):
    title_label = "Name"


class Paper(Entity):
    pass


class Papers(Theme):
    id, name, version, api_version = "papers", "Papers", 1, 4
    entities = [Person, Paper]
    relationships = [
        related("authors", Person, Paper, label="Papers", reverse_label="Authors", ordered=True),
        related("reviewers", Person, Paper),
    ]
    views = [
        DetailView(Paper, [Section.fields(), Section.related("authors")]),
        DetailView(Person, [Section.fields(), Section.related("authors")]),
    ]


EMPTY = TagTree([], {})


@dataclass
class Env:
    engine: Engine
    schema: ThemeSchema

    def ingest[T](self, fn: Callable[[IngestSession], T]) -> T:
        with self.engine.begin() as conn:
            ctx = IngestSession(conn, self.schema)
            result = fn(ctx)
            ctx.flush()
            return result

    def authors(self, paper: EntityRef) -> list[str]:
        with self.engine.begin() as conn:
            ctx = IngestSession(conn, self.schema)
            return [ctx.get(p).title for p in ctx.related("authors", paper)]

    def run[T](self, fn: Callable[[Connection], T]) -> T:
        with self.engine.begin() as conn:
            return fn(conn)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("papers", 1)))
    schema = open_theme(engine, keep, Papers).schema
    yield Env(engine, schema)
    engine.dispose()


def _people(ctx: IngestSession, *names: str) -> list[EntityRef]:
    return [ctx.upsert(Person, f"p:{n}", title=n) for n in names]


def _paper(env: Env, *authors: str) -> tuple[EntityRef, list[EntityRef]]:
    def make(ctx: IngestSession) -> tuple[EntityRef, list[EntityRef]]:
        paper = ctx.upsert(Paper, "paper:1", title="A Paper")
        people = _people(ctx, *authors)
        for n, person in enumerate(people):
            ctx.relate("authors", person, paper, position=n)
        return paper, people

    return env.ingest(make)


# --- declaring ---


def test_ordered_needs_many() -> None:
    with pytest.raises(ThemeDeclarationError, match="ordered=True needs many=True"):
        related("lead", Person, Paper, many=False, ordered=True)


def test_ordered_needs_api_4() -> None:
    class Old(Papers):
        api_version = 3

    assert validate_theme(Papers) == []
    assert validate_theme(Old) == [
        "a theme with ordered relationships must set api_version = 4 (or later)"
    ]


def test_an_ordered_link_table_has_a_position(env: Env) -> None:
    columns = {c["name"] for c in inspect(env.engine).get_columns("papers_authors")}
    assert columns == {"a_id", "b_id", "position"}
    plain = {c["name"] for c in inspect(env.engine).get_columns("papers_reviewers")}
    assert plain == {"a_id", "b_id"}


def test_a_relationship_made_ordered_gains_the_column(env: Env) -> None:
    class Later(Papers):
        relationships = [
            related("authors", Person, Paper, ordered=True),
            related("reviewers", Person, Paper, ordered=True),
        ]

    changes = env.run(lambda conn: sync_theme_schema(conn, build_theme_schema(Later)))
    assert changes.columns == ["papers_reviewers.position"]


# --- from files ---


def test_positions_order_a_papers_authors(env: Env) -> None:
    paper, people = _paper(env, "Zed", "Amy", "Max")
    assert env.authors(paper) == ["Zed", "Amy", "Max"]  # their order, not by name
    # From an author's side: their papers, in id order as before.
    assert env.ingest(lambda ctx: ctx.related("authors", people[0])) == [paper]
    # The file now names them in another order.

    def reorder(ctx: IngestSession) -> None:
        for n, person in enumerate([people[1], people[2], people[0]]):
            ctx.relate("authors", person, paper, position=n)

    env.ingest(reorder)
    assert env.authors(paper) == ["Amy", "Max", "Zed"]


def test_without_a_position_new_ones_go_last_and_others_stay(env: Env) -> None:
    paper, people = _paper(env, "Zed", "Amy")

    def more(ctx: IngestSession) -> None:
        [bea] = _people(ctx, "Bea")
        ctx.relate("authors", bea, paper)  # last
        ctx.relate("authors", people[0], paper)  # stays first

    env.ingest(more)
    assert env.authors(paper) == ["Zed", "Amy", "Bea"]


def test_a_position_on_an_unordered_relationship_is_an_error(env: Env) -> None:
    paper, people = _paper(env, "Zed")
    with pytest.raises(IngestError, match="isn't ordered"):
        env.ingest(lambda ctx: ctx.relate("reviewers", people[0], paper, position=0))


# --- by hand ---


def test_added_by_hand_goes_last(env: Env) -> None:
    paper, _ = _paper(env, "Zed", "Amy")
    env.run(lambda conn: add_related(conn, env.schema, "authors", paper.id, new_title="Bea"))
    assert env.authors(paper) == ["Zed", "Amy", "Bea"]


def test_moving_up_and_down(env: Env) -> None:
    paper, people = _paper(env, "A", "B", "C", "D")
    a, b, c, d = (p.id for p in people)
    change = env.run(lambda conn: move_related(conn, env.schema, "authors", paper.id, [c], -1))
    assert change.label == "Move up in A Paper's authors"
    assert env.authors(paper) == ["A", "C", "B", "D"]
    env.run(lambda conn: move_related(conn, env.schema, "authors", paper.id, [a, b], 1))
    assert env.authors(paper) == ["C", "A", "D", "B"]  # each down one, in their order
    env.run(lambda conn: move_related(conn, env.schema, "authors", paper.id, [b], 1))
    assert env.authors(paper) == ["C", "A", "D", "B"]  # already last: stays
    env.run(lambda conn: move_related(conn, env.schema, "authors", paper.id, [d, c], -3))
    assert env.authors(paper) == ["C", "D", "A", "B"]  # stopping at the top, not passing C
    with pytest.raises(RelationError, match="no order"):
        env.run(lambda conn: move_related(conn, env.schema, "reviewers", paper.id, [a], 1))


def test_a_hand_made_order_survives_scans(env: Env) -> None:
    paper, people = _paper(env, "A", "B", "C")
    env.run(lambda conn: move_related(conn, env.schema, "authors", paper.id, [people[2].id], -2))
    assert env.authors(paper) == ["C", "A", "B"]

    def rescan(ctx: IngestSession) -> None:
        for n, person in enumerate(people):  # the file's order again
            ctx.relate("authors", person, paper, position=n)
        [d] = _people(ctx, "D")
        ctx.relate("authors", d, paper, position=0)  # new, at the front in the file

    env.ingest(rescan)
    assert env.authors(paper) == ["C", "A", "B", "D"]  # the user's order; the new one last


def test_undo_puts_the_order_back(env: Env) -> None:
    paper, people = _paper(env, "A", "B", "C")
    change = env.run(
        lambda conn: move_related(conn, env.schema, "authors", paper.id, [people[2].id], -1)
    )
    assert env.authors(paper) == ["A", "C", "B"]
    env.run(lambda conn: restore_states(conn, env.schema, change.before))
    assert env.authors(paper) == ["A", "B", "C"]
    assert env.run(lambda conn: conn.scalar(select(UserOrder.b_id))) is None  # not by hand now
    env.run(lambda conn: restore_states(conn, env.schema, change.after))
    assert env.authors(paper) == ["A", "C", "B"]
    assert env.run(lambda conn: conn.scalar(select(UserOrder.b_id))) == paper.id


def test_removing_one_keeps_the_others_order(env: Env) -> None:
    paper, people = _paper(env, "A", "B", "C")
    env.run(lambda conn: remove_related(conn, env.schema, "authors", paper.id, [people[1].id]))
    assert env.authors(paper) == ["A", "C"]


# --- merging ---


def test_a_merged_author_keeps_their_place(env: Env) -> None:
    paper, people = _paper(env, "A", "B. Smith", "C")

    def more(ctx: IngestSession) -> EntityRef:
        [smith] = _people(ctx, "Bob Smith")  # the same person, another spelling
        return smith

    smith = env.ingest(more)
    env.run(lambda conn: merge_items(conn, env.schema, smith.id, [people[1].id]))
    assert env.authors(paper) == ["A", "Bob Smith", "C"]


def test_a_merged_papers_authors_go_last(env: Env) -> None:
    paper, people = _paper(env, "A", "B")

    def other(ctx: IngestSession) -> EntityRef:
        copy = ctx.upsert(Paper, "paper:2", title="A Paper (copy)")
        c, a = _people(ctx, "C", "A")
        ctx.relate("authors", c, copy, position=0)
        ctx.relate("authors", a, copy, position=1)
        return copy

    copy = env.ingest(other)
    env.run(lambda conn: move_related(conn, env.schema, "authors", copy.id, [people[0].id], -1))
    env.run(lambda conn: merge_items(conn, env.schema, paper.id, [copy.id]))
    assert env.authors(paper) == ["A", "B", "C"]
    # The copy's hand-made order came along: scans leave the kept paper's order alone.
    assert env.run(lambda conn: conn.scalar(select(UserOrder.b_id))) == paper.id


# --- pages and searches ---


def test_a_papers_page_lists_its_authors_in_order(env: Env) -> None:
    paper, people = _paper(env, "Zed", "Amy", "Max")
    detail = env.run(lambda conn: load_detail(conn, env.schema, paper.id, lambda _: None))
    assert detail is not None
    [section] = [s for s in detail.sections if s.kind == "related" and s.relationship == "authors"]
    assert [e.title for e in section.entities] == ["Zed", "Amy", "Max"]
    page = env.run(lambda conn: load_detail(conn, env.schema, people[0].id, lambda _: None))
    assert page is not None
    [papers] = [s for s in page.sections if s.relationship == "authors"]
    assert [e.title for e in papers.entities] == ["A Paper"]


def test_a_related_search_sorts_by_position(env: Env) -> None:
    paper, _ = _paper(env, "Zed", "Amy", "Max")
    fields = search_fields(env.schema, ("papers.person",))
    spec = SearchSpec(
        types=("papers.person",), related=("authors", paper.id), sort=(SortKey(POSITION),)
    )
    hits = env.run(lambda conn: run_search(conn, spec, EMPTY, fields=fields))
    assert [h.title for h in hits] == ["Zed", "Amy", "Max"]
    backwards = SearchSpec(
        types=("papers.person",),
        related=("authors", paper.id),
        sort=(SortKey(POSITION, descending=True),),
    )
    hits = env.run(lambda conn: run_search(conn, backwards, EMPTY, fields=fields))
    assert [h.title for h in hits] == ["Max", "Amy", "Zed"]
    unordered = SearchSpec(related=("reviewers", paper.id), sort=(SortKey(POSITION),))
    with pytest.raises(SearchError, match="has no order"):
        env.run(lambda conn: run_search(conn, unordered, EMPTY, fields=fields))
    with pytest.raises(SearchError, match="Only a related search"):
        env.run(lambda conn: run_search(conn, SearchSpec(sort=(SortKey(POSITION),)), EMPTY))
