"""Containers made by hand (#329), with the research theme's projects: making one, putting
papers in and taking them out, which scans, undo, merges, tags, and Within respect."""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import Connection, insert, select

from tagalot.builtin_themes.research import Paper, Project
from tagalot.core.containers import (
    ContainerError,
    contain_by_hand,
    containers_for,
    hand_made_types,
    new_container,
    uncontain_by_hand,
)
from tagalot.core.entity_state import restore_states
from tagalot.core.ingest import IngestSession
from tagalot.core.merge import merge_items
from tagalot.core.models import Entity, EntityContains, EntityTag
from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tags import TagTree
from tagalot.themes.api import EntityRef
from tests.themes.test_research import Env, env

__all__ = ["env"]  # the fixture

PROJECT, PAPER = "research.project", "research.paper"


def _run[T](env: Env, fn: Callable[[Connection], T]) -> T:
    return env.writer.run(fn)


def _new(env: Env, title: str) -> int:
    change, entity_id = _run(env, lambda c: new_container(c, env.schema, PROJECT, title))
    assert change.label == f"New project {' '.join(title.split())!r}"
    return entity_id


def _contents(env: Env, project: int) -> list[str]:
    with env.reader.connect() as conn:
        return sorted(
            conn.scalars(
                select(Entity.title)
                .join(EntityContains, EntityContains.child_id == Entity.id)
                .where(EntityContains.parent_id == project)
            )
        )


def test_the_theme_declares_projects_made_by_hand(env: Env) -> None:
    assert hand_made_types(env.schema) == [PROJECT]
    assert containers_for(env.schema, PAPER) == [PROJECT]
    assert containers_for(env.schema, "research.author") == []
    assert Project.made_by_hand
    assert not Paper.made_by_hand


def test_make_a_project_and_fill_it(env: Env) -> None:
    env.scan()
    thesis = _new(env, "  Thesis   reading ")
    with env.reader.connect() as conn:
        assert conn.scalar(select(Entity.title).where(Entity.id == thesis)) == "Thesis reading"
    attention = env.id_of("Attention Is All You Need")
    resnet = env.id_of("Deep Residual Learning for Image Recognition")
    change = _run(env, lambda c: contain_by_hand(c, env.schema, thesis, [attention, resnet]))
    assert change.label == "Add 2 items to Thesis reading"
    assert _contents(env, thesis) == [
        "Attention Is All You Need",
        "Deep Residual Learning for Image Recognition",
    ]
    # Within the project, as with any container.
    with env.reader.connect() as conn:
        hits = run_search(conn, SearchSpec(within=thesis), TagTree([], {}))
    assert sorted(h.title for h in hits) == _contents(env, thesis)
    change = _run(env, lambda c: uncontain_by_hand(c, env.schema, thesis, [resnet]))
    assert (
        change.label == "Remove 'Deep Residual Learning for Image Recognition' from Thesis reading"
    )
    assert _contents(env, thesis) == ["Attention Is All You Need"]


def test_refusals(env: Env) -> None:
    env.scan()
    thesis = _new(env, "Thesis")
    author = env.id_of("Ashish Vaswani")
    with pytest.raises(ContainerError, match="come from files"):
        _run(env, lambda c: new_container(c, env.schema, PAPER, "A paper by hand"))
    with pytest.raises(ContainerError, match="Type a name"):
        _run(env, lambda c: new_container(c, env.schema, PROJECT, "   "))
    with pytest.raises(ContainerError, match="can't go in Thesis"):
        _run(env, lambda c: contain_by_hand(c, env.schema, thesis, [author]))
    with pytest.raises(ContainerError, match="isn't in Thesis"):
        _run(env, lambda c: uncontain_by_hand(c, env.schema, thesis, [author]))


def test_scans_respect_what_the_user_did(env: Env) -> None:
    env.scan()
    thesis = _new(env, "Thesis")
    attention = env.id_of("Attention Is All You Need")
    resnet = env.id_of("Deep Residual Learning for Image Recognition")
    _run(env, lambda c: contain_by_hand(c, env.schema, thesis, [attention, resnet]))
    _run(env, lambda c: uncontain_by_hand(c, env.schema, thesis, [resnet]))

    def theme_tries(conn: Connection) -> None:
        ctx = IngestSession(conn, env.schema)
        project = EntityRef(thesis, PROJECT)
        ctx.uncontain(project, EntityRef(attention, PAPER))  # the user put it there: stays
        ctx.contain(project, EntityRef(resnet, PAPER))  # the user took it out: stays out
        ctx.flush()

    _run(env, theme_tries)
    assert _contents(env, thesis) == ["Attention Is All You Need"]


def test_undo_and_redo(env: Env) -> None:
    env.scan()
    thesis = _new(env, "Thesis")
    attention = env.id_of("Attention Is All You Need")
    change = _run(env, lambda c: contain_by_hand(c, env.schema, thesis, [attention]))
    _run(env, lambda c: restore_states(c, env.schema, change.before))
    assert _contents(env, thesis) == []
    _run(env, lambda c: restore_states(c, env.schema, change.after))
    assert _contents(env, thesis) == ["Attention Is All You Need"]


def test_merging_projects_keeps_their_papers(env: Env) -> None:
    env.scan()
    first, second = _new(env, "Reading"), _new(env, "Reading (old)")
    attention = env.id_of("Attention Is All You Need")
    resnet = env.id_of("Deep Residual Learning for Image Recognition")
    _run(env, lambda c: contain_by_hand(c, env.schema, first, [attention]))
    _run(env, lambda c: contain_by_hand(c, env.schema, second, [resnet]))
    _run(env, lambda c: merge_items(c, env.schema, first, [second]))
    assert _contents(env, first) == [
        "Attention Is All You Need",
        "Deep Residual Learning for Image Recognition",
    ]

    def theme_tries(conn: Connection) -> None:  # still the user's, after the merge
        ctx = IngestSession(conn, env.schema)
        ctx.uncontain(EntityRef(first, PROJECT), EntityRef(resnet, PAPER))
        ctx.flush()

    _run(env, theme_tries)
    assert len(_contents(env, first)) == 2


def test_a_projects_tag_counts_for_its_papers(env: Env) -> None:
    env.scan()
    thesis = _new(env, "Thesis")
    attention = env.id_of("Attention Is All You Need")
    _run(env, lambda c: contain_by_hand(c, env.schema, thesis, [attention]))

    def tag(conn: Connection) -> Any:
        from tagalot.core.tags import add_tag

        tag_id = add_tag(conn, None, "Chapter 2")
        conn.execute(insert(EntityTag).values(entity_id=thesis, tag_id=tag_id))
        return tag_id

    tag_id = _run(env, tag)
    with env.reader.connect() as conn:
        tree = TagTree.load(conn)
        hits = run_search(
            conn, SearchSpec(types=(PAPER,), include=(tag_id,), inherit_tags=True), tree
        )
    assert [h.title for h in hits] == ["Attention Is All You Need"]
