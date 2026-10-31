"""Inside linked places (#616): the files read inside an archive that is a place are
linked to those inside its first place's archive, when linking and at later scans, and
go back with Undo and Unlink; with the 2D assets theme."""

import copy
import importlib.util
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.keep import RootConfig
from tagalot.core.linked_folders import folder_of
from tagalot.core.models import Entity, EntityResource, LinkedPlace, Resource, ResourceStatus
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
ZIP = "Aurora Studio/UI pack.zip"
COPY = "Kenji Sato/UI pack.zip"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as opened:
        root = Path(opened.root_path("assets"))
        shutil.copy(root / ZIP, root / COPY)  # a link the share shows as a file
        yield opened


def _read_inside(session: KeepSession) -> None:
    config = copy.deepcopy(session.keep.config)
    config.roots[0].archives = ["**"]
    session.save_config(config)


def _resource(session: KeepSession, relpath: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Resource.id).where(Resource.relpath == relpath))
    assert found is not None
    return found


def _covers(session: KeepSession) -> int:
    with session.reader.connect() as conn:
        return len(conn.scalars(select(Entity.id).where(Entity.title == "cover.png")).all())


def _member_places(session: KeepSession) -> dict[str, str]:
    with session.reader.connect() as conn:
        paths = {i: p for i, p in conn.execute(select(Resource.id, Resource.relpath))}
        rows = conn.execute(
            select(LinkedPlace.resource_id, LinkedPlace.first_id)
            .join(Resource, Resource.id == LinkedPlace.resource_id)
            .where(Resource.parent_resource_id.is_not(None))
        )
        return {paths[a]: paths[b] for a, b in rows}


def test_linking_an_archive_links_what_was_read_inside_it(session: KeepSession) -> None:
    _read_inside(session)
    session.scan_all()
    assert _covers(session) == 2
    label = session.link_as_one([_resource(session, ZIP), _resource(session, COPY)])
    assert _covers(session) == 1
    places = _member_places(session)
    assert places[f"{COPY}/UI pack/cover.png"] == f"{ZIP}/UI pack/cover.png"
    assert len(places) == 4  # the cover and three buttons (the theme takes no .txt)
    session.scan_all()  # scans keep it so
    assert _covers(session) == 1
    assert len(_member_places(session)) == 4

    assert session.tags.undo() == label
    assert _covers(session) == 2
    assert _member_places(session) == {}
    assert session.tags.redo() == label
    assert _covers(session) == 1

    session.split_place(_resource(session, COPY))  # Unlink takes them along
    assert _member_places(session) == {}
    session.scan_all()  # read as files of their own again
    assert _covers(session) == 2


def test_files_read_inside_later_are_linked_by_the_scan(session: KeepSession) -> None:
    session.scan_all()
    session.link_as_one([_resource(session, ZIP), _resource(session, COPY)])
    assert _member_places(session) == {}  # nothing read inside yet
    _read_inside(session)
    session.scan_all()
    assert _covers(session) == 1
    assert len(_member_places(session)) == 4


def test_a_thumbnail_is_drawn_from_another_place_when_the_first_is_offline(
    session: KeepSession, tmp_path: Path
) -> None:
    root = Path(session.root_path("assets"))
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    shutil.copy(root / "Aurora Studio" / "Backgrounds" / "forest.png", mirror / "forest.png")
    config = copy.deepcopy(session.keep.config)
    config.roots.append(RootConfig("mirror", "Mirror", str(mirror), []))
    session.save_config(config)
    session.scan_all()
    forest = _resource(session, "Aurora Studio/Backgrounds/forest.png")
    session.link_as_one([forest, _resource(session, "forest.png")])
    with session.reader.connect() as conn:
        [item] = conn.scalars(select(Entity.id).where(Entity.title == "forest.png")).all()
    root.rename(root.with_name("away"))  # the first place's folder is offline
    session.scan_all()
    with session.reader.connect() as conn:
        status = conn.scalar(select(Resource.status).where(Resource.id == forest))
    assert status is ResourceStatus.OFFLINE
    session.thumbnails.cache.clear()
    result = session.thumbnails.resolve(item)
    assert result.thumbnail is not None  # drawn from the mirror
    assert result.resource_id == forest


def test_pictures_in_a_linked_gallery_folder_stay_the_first_places(tmp_path: Path) -> None:
    """The movies theme files a folder's pictures as its movie's and its own: linked as
    one with a copy, the copy's pictures add nothing of their own."""
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folder = "Home Movies/Alice/3rd Birthday"
    with KeepSession.open(module.make_movies_demo(tmp_path, notes=False), Settings()) as session:

        def files() -> set[tuple[int, str, str]]:
            with session.reader.connect() as conn:
                rows = conn.execute(
                    select(EntityResource.entity_id, Resource.relpath, EntityResource.role).join(
                        Resource, Resource.id == EntityResource.resource_id
                    )
                )
                return {(e, p, r) for e, p, r in rows}

        before = files()
        root = Path(session.root_path("movies"))
        shutil.copytree(root / folder, root / "Mirror" / "3rd Birthday")
        session.scan_all()
        with session.reader.connect() as conn:
            first = folder_of(conn, "movies", folder)
            other = folder_of(conn, "movies", "Mirror/3rd Birthday")
        session.link_folders(first, other)
        session.scan_all()
        assert files() == before  # nothing filed under the copy's paths
