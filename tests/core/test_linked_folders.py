"""Linked folders (#601): suggesting folders with the same name and matching files,
linking them as one, refusing files that differ, and linking files that turn up later,
with the 2D assets theme."""

import importlib.util
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.dedupe import Verifier
from tagalot.core.linked_folders import (
    Folder,
    confirm_same,
    folder_differences,
    folder_of,
    folder_pair,
    suggest_linked_folders,
)
from tagalot.core.models import Entity, LinkedFolder, LinkedPlace, Resource
from tagalot.core.place_links import PlaceError
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
FOLDER = "Aurora Studio/Backgrounds"
COPY = "Kenji Sato/Backgrounds"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as opened:
        root = Path(opened.root_path("assets"))
        shutil.copytree(root / FOLDER, root / COPY)  # a linked folder the share shows as one
        opened.scan_all()
        yield opened


def _root(session: KeepSession) -> Path:
    return Path(session.root_path("assets"))


def _folders(session: KeepSession) -> tuple[Folder, Folder]:
    with session.reader.connect() as conn:
        return folder_of(conn, "assets", FOLDER), folder_of(conn, "assets", COPY)


def _titled(session: KeepSession, title: str) -> list[int]:
    with session.reader.connect() as conn:
        return list(conn.scalars(select(Entity.id).where(Entity.title == title)))


def _linked(session: KeepSession) -> dict[str, str]:
    """Each linked place's path, and its first place's."""
    with session.reader.connect() as conn:
        rows = conn.execute(select(LinkedPlace.resource_id, LinkedPlace.first_id)).all()
        paths = {i: p for i, p in conn.execute(select(Resource.id, Resource.relpath))}
    return {paths[a]: paths[b] for a, b in rows}


def _folder_rows(session: KeepSession) -> list[tuple[str, str]]:
    with session.reader.connect() as conn:
        rows = conn.execute(select(LinkedFolder.relpath, LinkedFolder.first_relpath))
        return [(a, b) for a, b in rows]


def test_suggests_the_folders_and_previews_them(session: KeepSession) -> None:
    with session.reader.connect() as conn:
        pairs = suggest_linked_folders(conn)
    [pair] = pairs
    assert (pair.first.relpath, pair.other.relpath) == (FOLDER, COPY)
    assert {p.inside for p in pair.same} >= {"forest.png", "dusk sky.png"}
    assert pair.files_same == 2
    assert pair.differ == ()
    assert pair.only_first == pair.only_other == ()


def test_linking_makes_one_item_of_each_file_and_undoes(session: KeepSession) -> None:
    assert len(_titled(session, "forest.png")) == 2
    first, other = _folders(session)
    label = session.link_folders(first, other)
    assert label == "Link folders Backgrounds"
    assert len(_titled(session, "forest.png")) == 1
    assert len(_titled(session, "dusk sky.png")) == 1
    linked = _linked(session)
    assert linked[f"{COPY}/forest.png"] == f"{FOLDER}/forest.png"
    assert _folder_rows(session) == [(COPY, FOLDER)]
    session.scan_all()  # scans keep it so
    assert len(_titled(session, "forest.png")) == 1
    assert _linked(session) == linked  # the walk finds them as files: still linked
    with session.reader.connect() as conn:
        assert suggest_linked_folders(conn) == []  # linked: no longer suggested
    with pytest.raises(PlaceError, match="linked to a folder already"):
        session.link_folders(first, other)

    assert session.tags.undo() == label
    assert len(_titled(session, "forest.png")) == 2
    assert _linked(session) == {}
    assert _folder_rows(session) == []
    assert session.tags.redo() == label
    assert len(_titled(session, "forest.png")) == 1
    assert _folder_rows(session) == [(COPY, FOLDER)]


def test_files_that_differ_block_linking(session: KeepSession) -> None:
    changed = _root(session) / COPY / "forest.png"
    changed.write_bytes(changed.read_bytes()[:-10] + b"0123456789")
    session.scan_all()
    first, other = _folders(session)
    with session.reader.connect() as conn:
        pair = folder_pair(conn, first, other)
        [differ] = pair.differ
        assert differ.inside == "forest.png"
        # Read whole, they still differ: Check again doesn't clear them.
        assert confirm_same(conn, session.root_path, Verifier(), pair.differ) == set()
    with pytest.raises(PlaceError, match="1 file differs"):
        session.link_folders(first, other)
    # Found the same on a full read (a network glitch), they link.
    session.link_folders(first, other, {(differ.first_id, differ.other_id)})
    assert len(_titled(session, "forest.png")) == 1


def test_files_that_turn_up_later_are_linked_when_they_match(session: KeepSession) -> None:
    first, other = _folders(session)
    session.link_folders(first, other)
    root = _root(session)
    for folder in (FOLDER, COPY):
        shutil.copy(root / "Kenji Sato" / "tileset.png", root / folder / "tiles.png")
        shutil.copy(root / "Kenji Sato" / "sketch 01.jpg", root / folder / "sketch.jpg")
    odd = root / COPY / "sketch.jpg"
    odd.write_bytes(odd.read_bytes()[:-10] + b"0123456789")
    session.scan_all()
    assert len(_titled(session, "tiles.png")) == 1  # linked by the scan
    assert _linked(session)[f"{COPY}/tiles.png"] == f"{FOLDER}/tiles.png"
    assert len(_titled(session, "sketch.jpg")) == 2  # differs: not linked
    with session.reader.connect() as conn:
        [difference] = folder_differences(conn)
    assert difference.relpath == f"{COPY}/sketch.jpg"
    assert difference.first_shown.endswith(f"{FOLDER}/sketch.jpg")


def _shop(session: KeepSession) -> None:
    """A Shop01 folder with two pictures in both Backgrounds folders."""
    root = _root(session)
    for folder in (FOLDER, COPY):
        (root / folder / "Shop01").mkdir()
        shutil.copy(root / "Kenji Sato" / "tileset.png", root / folder / "Shop01" / "tiles.png")
        shutil.copy(root / "Kenji Sato" / "sketch 01.jpg", root / folder / "Shop01" / "a.jpg")


def test_a_folder_that_matches_inside_one_that_doesnt_is_suggested(
    session: KeepSession,
) -> None:
    _shop(session)
    extra = _root(session) / COPY / "extra.png"
    shutil.copy(_root(session) / "Kenji Sato" / "tileset.png", extra)
    extra.write_bytes(extra.read_bytes() + b"x")  # a file in one Backgrounds only
    session.scan_all()
    with session.reader.connect() as conn:
        pairs = suggest_linked_folders(conn)
    found = {(p.first.relpath, p.other.relpath): p for p in pairs}
    assert set(found) == {(FOLDER, COPY), (f"{FOLDER}/Shop01", f"{COPY}/Shop01")}
    assert found[FOLDER, COPY].only_other == ("extra.png",)
    shop = found[f"{FOLDER}/Shop01", f"{COPY}/Shop01"]
    assert {p.inside for p in shop.same} >= {"tiles.png", "a.jpg"}
    assert shop.only_first == shop.only_other == ()
    assert shop.differ == ()

    session.link_folders(shop.first, shop.other)  # Shop01 alone
    assert len(_titled(session, "tiles.png")) == 1
    assert len(_titled(session, "forest.png")) == 2  # Backgrounds isn't linked
    with session.reader.connect() as conn:
        left = [(p.first.relpath, p.other.relpath) for p in suggest_linked_folders(conn)]
    assert left == [(FOLDER, COPY)]


def test_a_folder_inside_a_pair_that_matches_completely_isnt_suggested_apart(
    session: KeepSession,
) -> None:
    _shop(session)
    session.scan_all()
    with session.reader.connect() as conn:
        pairs = suggest_linked_folders(conn)
    assert [(p.first.relpath, p.other.relpath) for p in pairs] == [(FOLDER, COPY)]
    assert {"Shop01/tiles.png", "Shop01/a.jpg"} <= {p.inside for p in pairs[0].same}
