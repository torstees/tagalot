"""Files inside archives on an item's page and in Triage's and Duplicates' rows (#524):
shown with their archive, opened from a copy, and shown in the file manager as the
archive."""

import importlib.util
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.detail import FileRow
from tagalot.core.handlers import OPEN, REVEAL
from tagalot.core.keep import load_keep_config, save_keep_config
from tagalot.core.member_paths import PATH_MARK
from tagalot.core.models import EntityResource, Resource
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.workers import ScanController
from tests.ui.test_contents_search import SCRIPT

pytestmark = pytest.mark.gui

PACK = "Aurora Studio/UI pack.zip"
COVER = f"{PACK}/UI pack/cover.png"


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keep = module.make_assets_demo(tmp_path)
    toml = keep / "keep.toml"
    config = load_keep_config(toml)
    config.roots = [replace(r, archives=["Aurora Studio/**"]) for r in config.roots]
    save_keep_config(config, toml)
    with KeepSession.open(keep, Settings()) as session:
        session.scan_all()
        window = MainWindow(session, scans=ScanController(QThreadPool()))
        qtbot.addWidget(window)
        window.resize(1200, 800)
        window.show()
        yield window
        window.thumbnails.clear()
        window.thumbnails.wait()


def _cover(session: KeepSession) -> tuple[int, int]:
    """The cover's resource id, and its item's."""
    with session.reader.connect() as conn:
        row = conn.execute(
            select(Resource.id, EntityResource.entity_id)
            .join(EntityResource, EntityResource.resource_id == Resource.id)
            .where(Resource.relpath == COVER)
        ).first()
    assert row is not None, "the cover inside the UI pack wasn't read"
    return row.id, row.entity_id


def _launched(qtbot: QtBot, launched: list[tuple[str, str]], count: int) -> tuple[str, str]:
    qtbot.waitUntil(lambda: len(launched) >= count, timeout=5000)
    return launched[count - 1]


def test_a_members_page_names_its_archive_and_opens_a_copy(
    qtbot: QtBot, window: MainWindow, nothing_is_launched: list[tuple[str, str]]
) -> None:
    session = window.session
    assert session is not None
    resource_id, entity_id = _cover(session)
    window.open_entity(entity_id)
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: page.detail is not None, timeout=5000)
    assert page.detail is not None

    # The file row: the archive's path, then the path inside it.
    shown = f"{PACK} {PATH_MARK} UI pack/cover.png"
    labels = [w for w in page.findChildren(QLabel) if shown in w.text()]
    assert labels, "no file row names the archive"
    archive = str(Path(session.root_path("assets")) / "Aurora Studio" / "UI pack.zip")
    assert labels[0].toolTip() == f"Inside the archive {Path(archive)}"

    files = [f for s in page.detail.sections for f in s.files if f.resource_id == resource_id]
    file = files[0]
    assert isinstance(file, FileRow)
    actions = {a.text(): a for a in page._file_menu(file).actions()}
    assert "Show the archive in file manager" in actions
    assert "Show in file manager" not in actions

    # Open file: a copy in the session's temporary folder, the archive untouched.
    before = Path(archive).read_bytes()
    actions["Open file"].trigger()
    how, path = _launched(qtbot, nothing_is_launched, 1)
    assert how == "open"
    copy = Path(path)
    assert copy.name == "cover.png"
    assert copy.is_relative_to(session.temp_dir())
    assert copy.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert Path(archive).read_bytes() == before

    # Show the archive in file manager: the archive itself.
    actions["Show the archive in file manager"].trigger()
    how, path = _launched(qtbot, nothing_is_launched, 2)
    assert (how, Path(path)) == ("reveal", Path(archive))


def test_a_row_of_any_list_opens_a_member_by_its_id(
    qtbot: QtBot, window: MainWindow, nothing_is_launched: list[tuple[str, str]]
) -> None:
    """Triage's and Duplicates' rows aren't one item's: they open by the file's id."""
    session = window.session
    assert session is not None
    resource_id, _ = _cover(session)
    window.files.act_on_resource(resource_id, OPEN)
    how, path = _launched(qtbot, nothing_is_launched, 1)
    assert (how, Path(path).name) == ("open", "cover.png")
    window.files.act_on_resource(resource_id, REVEAL)
    how, path = _launched(qtbot, nothing_is_launched, 2)
    assert (how, Path(path).name) == ("reveal", "UI pack.zip")


def test_a_member_that_cant_be_copied_says_so(
    qtbot: QtBot, window: MainWindow, nothing_is_launched: list[tuple[str, str]]
) -> None:
    session = window.session
    assert session is not None
    resource_id, _ = _cover(session)
    archive = Path(session.root_path("assets")) / "Aurora Studio" / "UI pack.zip"
    archive.write_bytes(b"not an archive any more")
    with qtbot.waitSignal(window.files.message, timeout=5000) as said:
        window.files.act_on_resource(resource_id, OPEN)
    assert str(said.args[0]).startswith("Couldn't open cover.png from ")
    assert nothing_is_launched == []
