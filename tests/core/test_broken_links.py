"""Broken links (#604): the walk reports links and shortcuts whose targets are gone, a
scan records them for Triage, and a scan that finds the target again clears them."""

import importlib.util
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tagalot.core.broken_links import Broken, broken_links, count_broken_links
from tagalot.core.scanner import walk_root
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tests.core.test_shortcuts import make_lnk

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
LINK = "Kenji Sato/gone.png.lnk"


def test_the_walk_reports_broken_links(tmp_path: Path) -> None:
    (tmp_path / "a.lnk").write_bytes(make_lnk(relative="..\\gone\\x.png"))
    try:
        os.symlink(tmp_path / "nowhere.png", tmp_path / "b.png")
        symlinks = True
    except OSError:
        symlinks = False  # creating symlinks needs extra privileges on this system
    found: list[tuple[str, str, str]] = []
    assert list(walk_root(str(tmp_path), on_broken=lambda *f: found.append(f))) == []
    expected = [("a.lnk", "shortcut", os.path.normpath(str(tmp_path / ".." / "gone" / "x.png")))]
    if symlinks:
        expected.append(("b.png", "symlink", os.path.realpath(tmp_path / "nowhere.png")))
    assert sorted(found) == expected


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as opened:
        yield opened


def _broken(session: KeepSession) -> list[Broken]:
    with session.reader.connect() as conn:
        found = broken_links(conn)
        assert count_broken_links(conn) == len(found)
    return found


def test_a_scan_records_them_until_the_target_is_back(session: KeepSession) -> None:
    root = Path(session.root_path("assets"))
    (root / LINK).write_bytes(make_lnk(relative="..\\Aurora Studio\\gone.png"))
    session.scan_all()
    [broken] = _broken(session)
    assert (broken.root_name, broken.relpath, broken.how) == ("Asset files", LINK, "shortcut")
    assert broken.pointed == os.path.normpath(str(root / "Aurora Studio" / "gone.png"))
    session.scan_all()
    assert len(_broken(session)) == 1  # still broken: listed once

    shutil.copy(root / "Kenji Sato" / "tileset.png", root / "Aurora Studio" / "gone.png")
    session.scan_all()  # the target is back
    assert _broken(session) == []
