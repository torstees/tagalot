"""Cast by hand (#260): adding and removing actors, kept through scans that read the
movie's .nfo again, and undone as one step."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import Connection, select

from tagalot.builtin_themes.movies import Actor
from tagalot.core.actions import ActionChange, restore_action
from tagalot.core.models import Entity, UserRelation
from tagalot.core.relations import RelationError, add_related, remove_related
from tests.themes.test_movies import T0, Env, _cast, env

__all__ = ["env"]  # fixtures

NFO = (
    "<movie><title>Heat</title>"
    "<actor><name>Al Pacino</name></actor><actor><name>Robert De Niro</name></actor></movie>"
)


def _id(env: Env, title: str) -> int:
    with env.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None, title
    return found


def _nfo_scan(env: Env, hours: int, text: str = NFO) -> None:
    (env.files / "Heat (1995).nfo").write_text(text + " " * hours, encoding="utf-8")
    env.scan(T0 + timedelta(hours=hours))


def _run(env: Env, op: Callable[[Connection], ActionChange]) -> ActionChange:
    return env.writer.run(op)


def test_an_actor_added_by_hand_stays_in_the_cast(env: Env) -> None:
    _nfo_scan(env, 0)
    _nfo_scan(env, 1, NFO.replace("<actor><name>Robert De Niro</name></actor>", ""))
    # (De Niro left the cast and was deleted; the .nfo of Inception has no cast)
    (env.files / "Alien (1979).nfo").write_text(
        "<movie><actor><name>Val Kilmer</name></actor></movie>", encoding="utf-8"
    )
    env.scan(T0 + timedelta(hours=2))
    heat, kilmer = _id(env, "Heat"), _id(env, "Val Kilmer")
    change = _run(env, lambda c: add_related(c, env.schema, "cast", heat, kilmer))
    assert change.label == "Add 'Val Kilmer' to Heat's cast"
    assert _cast(env, "Heat") == ["Al Pacino", "Val Kilmer"]
    _nfo_scan(env, 3)  # the .nfo doesn't name him: he stays, as the user's
    assert _cast(env, "Heat") == ["Al Pacino", "Robert De Niro", "Val Kilmer"]
    with env.reader.connect() as conn:
        assert conn.execute(select(UserRelation.added)).scalars().all() == [True]


def test_an_actor_removed_by_hand_stays_out(env: Env) -> None:
    _nfo_scan(env, 0)
    heat, de_niro = _id(env, "Heat"), _id(env, "Robert De Niro")
    change = _run(env, lambda c: remove_related(c, env.schema, "cast", heat, de_niro))
    assert change.label == "Remove 'Robert De Niro' from Heat's cast"
    assert _cast(env, "Heat") == ["Al Pacino"]
    _nfo_scan(env, 1)  # read again: he isn't brought back
    assert _cast(env, "Heat") == ["Al Pacino"]
    assert "Robert De Niro" in env.titles(Actor)  # the theme deletes only actors it drops

    env.writer.run(lambda c: restore_action(c, env.schema, change, forward=False))
    assert _cast(env, "Heat") == ["Al Pacino", "Robert De Niro"]
    with env.reader.connect() as conn:
        assert conn.scalar(select(UserRelation.name)) is None  # the record went too
    env.writer.run(lambda c: restore_action(c, env.schema, change, forward=True))
    assert _cast(env, "Heat") == ["Al Pacino"]


def test_a_new_actor_made_by_hand(env: Env) -> None:
    _nfo_scan(env, 0)
    heat = _id(env, "Heat")
    change = _run(
        env, lambda c: add_related(c, env.schema, "cast", heat, new_title="  Ashley   Judd ")
    )
    assert change.label == "Add 'Ashley Judd' to Heat's cast"
    judd = _id(env, "Ashley Judd")
    with env.reader.connect() as conn:
        assert conn.scalar(select(Entity.ingest_key).where(Entity.id == judd)) is None
    # Out of every cast, she still isn't deleted by a scan: the user made her.
    _run(env, lambda c: remove_related(c, env.schema, "cast", heat, judd))
    _nfo_scan(env, 1)
    assert "Ashley Judd" in env.titles(Actor)

    # Undoing the adding undoes the making.
    env.writer.run(lambda c: restore_action(c, env.schema, change, forward=False))
    assert "Ashley Judd" not in env.titles(Actor)


def test_from_the_actors_side(env: Env) -> None:
    _nfo_scan(env, 0)
    pacino, alien = _id(env, "Al Pacino"), _id(env, "Alien")
    change = _run(env, lambda c: add_related(c, env.schema, "cast", pacino, alien))
    assert change.label == "Add 'Alien' to Al Pacino's filmography"
    assert _cast(env, "Alien") == ["Al Pacino"]


def test_what_cant_be_related(env: Env) -> None:
    _nfo_scan(env, 0)
    heat, alien, pacino = _id(env, "Heat"), _id(env, "Alien"), _id(env, "Al Pacino")

    def fails(op: Callable[[Connection], ActionChange], match: str) -> None:
        with pytest.raises(RelationError, match=match):
            env.writer.run(op)

    fails(lambda c: add_related(c, env.schema, "cast", heat, alien), "can't be added")
    fails(lambda c: add_related(c, env.schema, "crew", heat, pacino), "no 'crew'")
    fails(lambda c: add_related(c, env.schema, "cast", heat, new_title="  "), "Type a name")
    fails(lambda c: add_related(c, env.schema, "cast", heat, 999_999), "no longer exists")
    fails(lambda c: remove_related(c, env.schema, "cast", alien, pacino), "isn't related")
