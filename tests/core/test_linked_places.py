"""Linked places (#598): one file in several places is one item, filed where each place's
path puts it, with the 2D assets theme (an artist is the first folder of a file's path)."""

import importlib.util
import shutil
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.dedupe import exact_groups
from tagalot.core.detail import role_files
from tagalot.core.handlers import choose_file, files_to_open
from tagalot.core.models import (
    Entity,
    EntityContains,
    EntityResource,
    LinkedPlace,
    PlaceContains,
    Resource,
    ResourceStatus,
)
from tagalot.core.place_links import PlaceError, link_place
from tagalot.core.places import first_place, places_of
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.triage import MISSING, triage_condition, unlinked_files

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
FOREST = "Aurora Studio/Backgrounds/forest.png"
LINK = "Kenji Sato/forest.png"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as opened:
        yield opened


def _root(session: KeepSession) -> Path:
    return Path(session.root_path("assets"))


def _resource(session: KeepSession, relpath: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Resource.id).where(Resource.relpath == relpath))
    assert found is not None, relpath
    return found


def _titled(session: KeepSession, title: str) -> list[int]:
    with session.reader.connect() as conn:
        return list(conn.scalars(select(Entity.id).where(Entity.title == title)))


def _artists(session: KeepSession, entity_id: int) -> set[str]:
    """The artists holding an item, by folder name (a note may retitle one)."""
    with session.reader.connect() as conn:
        titles = conn.scalars(
            select(Entity.title)
            .join(EntityContains, EntityContains.parent_id == Entity.id)
            .where(EntityContains.child_id == entity_id)
        )
        return {t.partition(" (")[0] for t in titles}


def _link(session: KeepSession, place: str, first: str, how: str = "user") -> None:
    session.writer.run(
        partial(
            link_place,
            schema=session.schema,
            place_id=_resource(session, place),
            first_id=_resource(session, first),
            how=how,
        )
    )


def test_a_place_read_after_linking_files_the_item_under_both_artists(
    session: KeepSession,
) -> None:
    session.scan_all()
    [forest] = _titled(session, "forest.png")
    shutil.copy(_root(session) / FOREST, _root(session) / LINK)
    session.scan_all()
    assert len(_titled(session, "forest.png")) == 2  # not linked: two items
    _link(session, LINK, FOREST)
    session.scan_all()
    assert _titled(session, "forest.png") == [forest]  # the copy's item went into it
    assert _artists(session, forest) == {"Aurora Studio", "Kenji Sato"}
    with session.reader.connect() as conn:
        assert first_place(conn, _resource(session, LINK)) == _resource(session, FOREST)
        assert places_of(conn, [_resource(session, FOREST)]) == {
            _resource(session, FOREST): [_resource(session, LINK)]
        }
        # The place is no file of any item, and is filed as the place's.
        assert (
            conn.scalar(
                select(EntityResource.entity_id).where(
                    EntityResource.resource_id == _resource(session, LINK)
                )
            )
            is None
        )
        assert conn.execute(select(PlaceContains.resource_id)).scalars().all() == [
            _resource(session, LINK)
        ]


def test_reading_either_place_again_keeps_both_artists(session: KeepSession) -> None:
    session.scan_all()
    _link_first_read(session)
    [forest] = _titled(session, "forest.png")
    for relpath in (FOREST, LINK):  # changed: read again
        path = _root(session) / relpath
        path.write_bytes(path.read_bytes() + b"x")
        session.scan_all()
        assert _titled(session, "forest.png") == [forest]
        assert _artists(session, forest) == {"Aurora Studio", "Kenji Sato"}


def _link_first_read(session: KeepSession) -> None:
    shutil.copy(_root(session) / FOREST, _root(session) / LINK)
    session.scan_all()
    _link(session, LINK, FOREST)
    session.scan_all()


def test_a_place_moved_to_another_artist_takes_its_filing_along(session: KeepSession) -> None:
    session.scan_all()
    _link_first_read(session)
    [forest] = _titled(session, "forest.png")
    moved = _root(session) / "Mira" / "forest.png"
    moved.parent.mkdir()
    (_root(session) / LINK).rename(moved)
    session.scan_all()
    assert _titled(session, "forest.png") == [forest]
    assert _artists(session, forest) == {"Aurora Studio", "Mira"}
    with session.reader.connect() as conn:
        assert first_place(conn, _resource(session, "Mira/forest.png")) == _resource(
            session, FOREST
        )


def test_pages_list_places_and_opening_finds_one_that_is_there(session: KeepSession) -> None:
    session.scan_all()
    _link_first_read(session)
    [forest] = _titled(session, "forest.png")
    (_root(session) / FOREST).unlink()
    session.scan_all()
    with session.reader.connect() as conn:
        files = role_files(conn, forest, "file", session.root_path)
        assert [(f.relpath, f.status, f.place_of) for f in files] == [
            (FOREST, ResourceStatus.MISSING, None),
            (LINK, ResourceStatus.OK, _resource(session, FOREST)),
        ]
        chosen = choose_file(files_to_open(conn, session.schema, forest, session.root_path))
        assert chosen.resource_id == _resource(session, LINK)
        # Missing only when every place is: the item isn't in Triage's missing files.
        missing = conn.scalars(select(Entity.id).where(triage_condition(MISSING))).all()
        assert forest not in missing
        # A place isn't an unlinked file either.
        assert LINK not in {f.relpath for f in unlinked_files(conn)}


def test_places_of_one_file_are_not_duplicates(session: KeepSession) -> None:
    session.scan_all()
    shutil.copy(_root(session) / FOREST, _root(session) / LINK)
    session.scan_all()
    with session.reader.connect() as conn:
        before = [
            g
            for g in exact_groups(conn, session.root_path)
            if any(f.relpath == LINK for f in g.files)
        ]
    assert len(before) == 1
    _link(session, LINK, FOREST)
    session.scan_all()
    with session.reader.connect() as conn:
        after = [
            g
            for g in exact_groups(conn, session.root_path)
            if any(f.relpath == FOREST for f in g.files)
        ]
    assert after == []


def test_linking_rules(session: KeepSession) -> None:
    session.scan_all()
    shutil.copy(_root(session) / FOREST, _root(session) / LINK)
    session.scan_all()
    with pytest.raises(PlaceError, match="itself"):
        _link(session, FOREST, FOREST)
    with pytest.raises(PlaceError, match="a folder to a folder"):
        _link(session, LINK, "Aurora Studio")
    # Linking to a linked place links to its group's first place; a first place linked
    # elsewhere brings its places along.
    third = _root(session) / "Kenji Sato" / "forest again.png"
    shutil.copy(_root(session) / FOREST, third)
    session.scan_all()
    _link(session, LINK, FOREST)
    _link(session, "Kenji Sato/forest again.png", LINK)
    with session.reader.connect() as conn:
        firsts = set(conn.scalars(select(LinkedPlace.first_id)))
    assert firsts == {_resource(session, FOREST)}
    session.scan_all()
    assert len(_titled(session, "forest.png")) == 1
    assert _titled(session, "forest again.png") == []
