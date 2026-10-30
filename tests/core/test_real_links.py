"""Real links linked automatically (#599): the walk through symbolic links, junctions, and
hard links, and scans making them linked places, with the 2D assets theme."""

import importlib.util
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core import scanjob
from tagalot.core.models import Entity, EntityContains, LinkedPlace, Resource
from tagalot.core.real_links import _where
from tagalot.core.scanner import walk_root
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
FOREST = "Aurora Studio/Backgrounds/forest.png"


def _symlink(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("creating symlinks needs extra privileges on this system")


def _junction(target: Path, link: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are Windows' own")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def _walk(root: Path, **kwargs: object) -> dict[str, tuple[str | None, str | None]]:
    """Each path the walk lists, with how it was reached through a link and its target's
    name relative to the root (or its full path outside)."""
    real = os.path.realpath(root)
    found = {}
    for entry in walk_root(str(root), dirs=True, **kwargs):  # type: ignore[arg-type]
        target = entry.target
        if target is not None and os.path.normcase(target).startswith(os.path.normcase(real)):
            target = Path(os.path.relpath(target, real)).as_posix()
        found[entry.relpath] = (entry.how, target)
    return found


# --- the walk ---


def test_a_linked_folder_is_walked_with_the_real_paths_inside(tmp_path: Path) -> None:
    (tmp_path / "Real" / "Sub").mkdir(parents=True)
    (tmp_path / "Real" / "Sub" / "a.png").write_bytes(b"a")
    (tmp_path / "Other").mkdir()
    _symlink(tmp_path / "Real", tmp_path / "Other" / "Linked")
    (tmp_path / "file.png").write_bytes(b"f")
    _symlink(tmp_path / "file.png", tmp_path / "Other" / "file link.png")
    found = _walk(tmp_path)
    assert found["Real/Sub/a.png"] == (None, None)
    assert found["Other/Linked"] == ("symlink", "Real")
    assert found["Other/Linked/Sub"] == ("inside", "Real/Sub")
    assert found["Other/Linked/Sub/a.png"] == ("inside", "Real/Sub/a.png")
    assert found["Other/file link.png"] == ("symlink", "file.png")


def test_junctions_are_walked_as_folder_links(tmp_path: Path) -> None:
    (tmp_path / "Real").mkdir()
    (tmp_path / "Real" / "a.png").write_bytes(b"a")
    _junction(tmp_path / "Real", tmp_path / "Junction")
    found = _walk(tmp_path)
    assert found["Junction"] == ("junction", "Real")
    assert found["Junction/a.png"] == ("inside", "Real/a.png")


def test_loops_are_not_walked(tmp_path: Path) -> None:
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "A" / "a.png").write_bytes(b"a")
    _symlink(tmp_path, tmp_path / "A" / "up")  # back to the root
    _symlink(tmp_path / "B", tmp_path / "A" / "to B")
    _symlink(tmp_path / "A", tmp_path / "B" / "to A")  # A and B lead to each other
    found = _walk(tmp_path)
    assert "A/up" not in found
    assert found["A/to B"] == ("symlink", "B")
    assert "A/to B/to A" not in found  # back to A, which the walk is inside
    assert found["B/to A"] == ("symlink", "A")
    assert "B/to A/to B" not in found
    assert len(found) < 20


def test_links_outside_the_given_folders_and_broken_links_are_left_out(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "x.png").write_bytes(b"x")
    gone = tmp_path / "gone.png"
    gone.write_bytes(b"g")
    _symlink(outside, tmp_path / "out")
    _symlink(gone, tmp_path / "broken.png")
    gone.unlink()
    assert _walk(tmp_path) == {}
    # Another of the keep's folders may be walked.
    both = _walk(tmp_path, link_roots=[os.path.realpath(tmp_path), os.path.realpath(outside)])
    assert both["out"][0] == "symlink"
    assert both["out/x.png"][0] == "inside"


def test_hard_links_say_which_file_they_are(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_bytes(b"a")
    os.link(tmp_path / "a.png", tmp_path / "b.png")
    (tmp_path / "c.png").write_bytes(b"c")
    ids = {e.relpath: e.file_id for e in walk_root(str(tmp_path), hard_links=True)}
    assert ids["a.png"] is not None
    assert ids["a.png"] == ids["b.png"]
    assert ids["c.png"] is None
    assert all(e.file_id is None for e in walk_root(str(tmp_path)))  # not asked


def test_real_paths_are_placed_in_the_keeps_roots(tmp_path: Path) -> None:
    music = os.path.normcase(os.path.normpath(tmp_path / "Music"))
    inner = os.path.normcase(os.path.normpath(tmp_path / "Music" / "Live"))
    roots = [(inner, "live"), (music, "music")]  # deepest first
    assert _where(str(tmp_path / "Music" / "A" / "b.mp3"), roots) == ("music", "A/b.mp3")
    assert _where(str(tmp_path / "Music" / "Live" / "c.mp3"), roots) == ("live", "c.mp3")
    assert _where(str(tmp_path / "Music"), roots) == ("music", "")
    assert _where(str(tmp_path / "Musical" / "d.mp3"), roots) is None


# --- scans ---


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


def _titled(session: KeepSession, title: str) -> list[int]:
    with session.reader.connect() as conn:
        return list(conn.scalars(select(Entity.id).where(Entity.title == title)))


def _artists(session: KeepSession, entity_id: int) -> set[str]:
    with session.reader.connect() as conn:
        titles = conn.scalars(
            select(Entity.title)
            .join(EntityContains, EntityContains.parent_id == Entity.id)
            .where(EntityContains.child_id == entity_id)
        )
        return {t.partition(" (")[0] for t in titles}


def _hows(session: KeepSession) -> dict[str, str]:
    with session.reader.connect() as conn:
        rows = conn.execute(
            select(Resource.relpath, LinkedPlace.how).join(
                Resource, Resource.id == LinkedPlace.resource_id
            )
        )
        return {relpath: how for relpath, how in rows}


def test_a_symbolic_link_to_a_file_is_a_place_of_it(session: KeepSession) -> None:
    session.scan_all()
    [forest] = _titled(session, "forest.png")
    _symlink(_root(session) / FOREST, _root(session) / "Kenji Sato" / "forest.png")
    [report] = session.scan_all()
    assert report.linked == 1
    assert _hows(session) == {"Kenji Sato/forest.png": "symlink"}
    assert _titled(session, "forest.png") == [forest]
    assert _artists(session, forest) == {"Aurora Studio", "Kenji Sato"}
    [again] = session.scan_all()
    assert again.linked == 0  # already linked


def test_a_link_already_read_as_its_own_item_is_folded_in(
    session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keep scanned before #599 read a symbolic link to a file as a file of its own."""
    session.scan_all()
    [forest] = _titled(session, "forest.png")
    _symlink(_root(session) / FOREST, _root(session) / "Kenji Sato" / "forest.png")
    with monkeypatch.context() as patched:
        patched.setattr(scanjob, "sync_links", lambda conn, **kwargs: 0)  # as before #599
        session.scan_all()
    assert len(_titled(session, "forest.png")) == 2
    session.scan_all()
    assert _titled(session, "forest.png") == [forest]
    assert _artists(session, forest) == {"Aurora Studio", "Kenji Sato"}


def test_a_linked_folder_files_everything_in_it_under_both(session: KeepSession) -> None:
    session.scan_all()
    [forest], [dusk] = _titled(session, "forest.png"), _titled(session, "dusk sky.png")
    _symlink(
        _root(session) / "Aurora Studio" / "Backgrounds",
        _root(session) / "Kenji Sato" / "Backgrounds",
    )
    session.scan_all()
    assert _hows(session) == {
        "Kenji Sato/Backgrounds": "symlink",  # the theme records folders too
        "Kenji Sato/Backgrounds/dusk sky.png": "inside",
        "Kenji Sato/Backgrounds/forest.png": "inside",
    }
    for item in (forest, dusk):
        assert _artists(session, item) == {"Aurora Studio", "Kenji Sato"}
    assert len(_titled(session, "forest.png")) == 1


def test_hard_links_are_places_of_one_file(session: KeepSession) -> None:
    session.scan_all()
    [forest] = _titled(session, "forest.png")
    os.link(_root(session) / FOREST, _root(session) / "Kenji Sato" / "forest.png")
    session.scan_all()
    assert _hows(session) == {"Kenji Sato/forest.png": "hardlink"}
    assert _titled(session, "forest.png") == [forest]
    assert _artists(session, forest) == {"Aurora Studio", "Kenji Sato"}


def test_a_link_replaced_by_a_file_is_unlinked(session: KeepSession) -> None:
    session.scan_all()
    link = _root(session) / "Kenji Sato" / "forest.png"
    _symlink(_root(session) / FOREST, link)
    session.scan_all()
    [forest] = _titled(session, "forest.png")
    link.unlink()
    shutil.copy(_root(session) / FOREST, link)  # a copy of its own now
    [report] = session.scan_all()
    assert report.linked == 1
    assert _hows(session) == {}
    assert len(_titled(session, "forest.png")) == 2
    assert _artists(session, forest) == {"Aurora Studio"}
