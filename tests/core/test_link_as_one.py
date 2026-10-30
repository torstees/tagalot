"""Link as one and Unlink (#600): linking copies by hand, separating a place again, and
undoing both, with the 2D assets theme."""

import importlib.util
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.models import Entity, EntityContains, EntityTag, LinkedPlace, Resource
from tagalot.core.place_links import PlaceError
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import add_tag_path, tag_entities

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
FOREST = "Aurora Studio/Backgrounds/forest.png"
COPY = "Kenji Sato/forest.png"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as opened:
        root = Path(opened.root_path("assets"))
        shutil.copy(root / FOREST, root / COPY)  # a link the share shows as a file
        opened.scan_all()
        yield opened


def _resource(session: KeepSession, relpath: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Resource.id).where(Resource.relpath == relpath))
    assert found is not None
    return found


def _items(session: KeepSession) -> list[int]:
    with session.reader.connect() as conn:
        return list(conn.scalars(select(Entity.id).where(Entity.title == "forest.png")))


def _artists(session: KeepSession, entity_id: int) -> set[str]:
    with session.reader.connect() as conn:
        titles = conn.scalars(
            select(Entity.title)
            .join(EntityContains, EntityContains.parent_id == Entity.id)
            .where(EntityContains.child_id == entity_id)
        )
        return {t.partition(" (")[0] for t in titles}


def _tags(session: KeepSession, entity_id: int) -> set[int]:
    with session.reader.connect() as conn:
        return set(conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id == entity_id)))


def _linked(session: KeepSession) -> set[tuple[int, int, str]]:
    with session.reader.connect() as conn:
        rows = conn.execute(select(LinkedPlace.resource_id, LinkedPlace.first_id, LinkedPlace.how))
        return {(a, b, c) for a, b, c in rows}


def _tag(session: KeepSession, entity_id: int, name: str) -> int:
    def job(conn: object) -> int:
        tag = add_tag_path(conn, [name])  # type: ignore[arg-type]
        tag_entities(conn, [entity_id], [tag])  # type: ignore[arg-type]
        return tag

    return session.writer.run(job)


def test_link_as_one_makes_one_item_in_both_places_and_undoes(session: KeepSession) -> None:
    forest, copy = _items(session)
    sky = _tag(session, copy, "Sky")
    label = session.link_as_one([_resource(session, FOREST), _resource(session, COPY)])
    assert label == "Link 2 copies as one"
    assert _items(session) == [forest]
    assert _artists(session, forest) == {"Aurora Studio", "Kenji Sato"}
    assert sky in _tags(session, forest)  # the copy's tag came along
    assert _linked(session) == {(_resource(session, COPY), _resource(session, FOREST), "user")}
    session.scan_all()  # scans keep it so
    assert _items(session) == [forest]
    assert _artists(session, forest) == {"Aurora Studio", "Kenji Sato"}

    assert session.tags.undo() == label
    assert _items(session) == [forest, copy]
    assert _linked(session) == set()
    assert _tags(session, copy) == {sky}
    assert _artists(session, forest) == {"Aurora Studio"}
    assert _artists(session, copy) == {"Kenji Sato"}
    assert session.tags.redo() == label
    assert _items(session) == [forest]
    assert _artists(session, forest) == {"Aurora Studio", "Kenji Sato"}
    with pytest.raises(PlaceError, match="linked already"):
        session.link_as_one([_resource(session, FOREST), _resource(session, COPY)])


def test_unlink_makes_an_item_of_its_own_and_undoes(session: KeepSession) -> None:
    forest, _ = _items(session)
    session.link_as_one([_resource(session, FOREST), _resource(session, COPY)])
    sky = _tag(session, forest, "Sky")
    label = session.split_place(_resource(session, COPY))
    assert label == "Unlink forest.png"
    [kept, separated] = _items(session)
    assert kept == forest
    assert _artists(session, forest) == {"Aurora Studio"}
    assert _artists(session, separated) == {"Kenji Sato"}
    assert sky in _tags(session, separated)
    assert _tags(session, separated) == _tags(session, forest)  # a copy of the shared tags
    assert _linked(session) == set()
    session.scan_all()  # read as a file of its own: its item stays the one
    assert _items(session) == [forest, separated]

    assert session.tags.undo() == label
    assert _items(session) == [forest]
    assert _artists(session, forest) == {"Aurora Studio", "Kenji Sato"}
    assert _linked(session) == {(_resource(session, COPY), _resource(session, FOREST), "user")}
    assert session.tags.redo() == label
    assert len(_items(session)) == 2
    with pytest.raises(PlaceError, match="isn't linked"):
        session.split_place(_resource(session, COPY))
