"""Choosing what to read inside an archive, by hand (#519): the dialog, the archive's
commands, Hide inside its archive, a folder's archives, and Keep configuration's box."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QModelIndex, Qt, QThreadPool
from PySide6.QtGui import QFocusEvent
from PySide6.QtWidgets import QApplication, QMenu
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.detail import FileRow
from tagalot.core.models import Entity, EntityResource, Resource
from tagalot.core.search import SearchHit
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.ui_state import load_ui_state
from tagalot.ui.archive_actions import (
    CHOOSE,
    HIDE,
    READ_ALL,
    READ_FOLDER,
    STOP,
    InsideArchiveMenu,
)
from tagalot.ui.archive_dialog import HIDE as HIDE_COLUMN
from tagalot.ui.archive_dialog import NAME, ChooseInsideDialog
from tagalot.ui.dedupe_view import DedupePage
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import ScanController
from tests.ui.test_contents_search import SCRIPT

pytestmark = pytest.mark.gui

PACK = "Aurora Studio/UI pack.zip"
INSIDE = {
    f"{PACK}/UI pack/{n}" for n in ("cover.png", "button 1.png", "button 2.png", "button 3.png")
}


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as session:
        window = MainWindow(session, scans=ScanController(QThreadPool()))
        qtbot.addWidget(window)
        window.resize(1200, 800)
        window.show()
        yield window
        if window.archives.dialog is not None:
            window.archives.dialog.reject()
        window.thumbnails.clear()
        window.thumbnails.wait()


def _session(window: MainWindow) -> KeepSession:
    assert window.session is not None
    return window.session


def _read(window: MainWindow) -> set[str]:
    """Files inside the archive read now: not skipped, with an item."""
    with _session(window).reader.connect() as conn:
        rows = conn.scalars(
            select(Resource.relpath)
            .join(EntityResource, EntityResource.resource_id == Resource.id)
            .where(Resource.parent_resource_id.is_not(None), Resource.skipped.is_(False))
        )
        return set(rows)


def _id(window: MainWindow, relpath: str) -> int:
    with _session(window).reader.connect() as conn:
        found = conn.scalar(select(Resource.id).where(Resource.relpath == relpath))
    assert found is not None, relpath
    return found


def _item(window: MainWindow, title: str) -> int:
    with _session(window).reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title.startswith(title)))
    assert found is not None, title
    return found


def _scan_after(qtbot: QtBot, window: MainWindow, act: object) -> None:
    """Do ``act`` (a callable), which saves a choice and scans."""
    with qtbot.waitSignal(window.scans.finished, timeout=20_000):
        act()  # type: ignore[operator]


def _page(qtbot: QtBot, window: MainWindow, title: str) -> DetailPage:
    window.open_entity(_item(window, title))
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: page.detail is not None, timeout=5000)
    return page


def _file_actions(page: DetailPage, relpath: str) -> dict[str, object]:
    assert page.detail is not None
    file = next(f for s in page.detail.sections for f in s.files if f.relpath == relpath)
    assert isinstance(file, FileRow)
    return {a.text(): a for a in page._file_menu(file).actions()}


def _dialog(qtbot: QtBot, window: MainWindow) -> ChooseInsideDialog:
    window.archives.choose(_id(window, PACK))
    qtbot.waitUntil(lambda: window.archives.dialog is not None, timeout=5000)
    dialog = window.archives.dialog
    assert dialog is not None
    qtbot.waitUntil(lambda: bool(dialog.files) or dialog.error is not None, timeout=5000)
    return dialog


def _row(dialog: ChooseInsideDialog, text: str) -> QModelIndex:
    flags = Qt.MatchFlag.MatchStartsWith | Qt.MatchFlag.MatchRecursive
    found = dialog.model.match(
        dialog.model.index(0, NAME), Qt.ItemDataRole.DisplayRole, text, -1, flags
    )
    assert found, text
    return found[0]


def test_choosing_what_to_read_inside(
    qtbot: QtBot, window: MainWindow, nothing_is_launched: list[tuple[str, str]]
) -> None:
    assert _read(window) == set()  # the demo reads no archive
    with qtbot.waitSignal(window.archives.message, timeout=5000) as said:
        dialog = _dialog(qtbot, window)
    assert said.args[0] == "1 file Tagalot doesn't read here: .txt (inside UI pack.zip)."
    assert dialog.error is None
    assert set(dialog.files) == {p.removeprefix(f"{PACK}/") for p in INSIDE}
    assert dialog.left_out == ["UI pack/license.txt"]
    assert dialog.title.toolTip() == "1 file Tagalot doesn't read here: .txt"
    assert dialog.hidden() == set()  # the first time, everything is shown
    assert dialog.summary.text().startswith("4 of 4 files will be read.")

    # The folder's ✕ hides everything under it; again, shows it all.
    folder = _row(dialog, "UI pack")
    dialog._clicked(folder.siblingAtColumn(HIDE_COLUMN))
    assert dialog.shown() == set()
    dialog._clicked(folder.siblingAtColumn(HIDE_COLUMN))
    assert len(dialog.shown()) == 4

    # A file's ✕ hides it: struck through, still listed; Show hidden files tucks it away.
    button = _row(dialog, "button 1.png")
    dialog._clicked(button.siblingAtColumn(HIDE_COLUMN))
    assert dialog.hidden() == {"UI pack/button 1.png"}
    item = dialog.model.itemFromIndex(button)
    assert item.font().strikeOut()
    assert not dialog.tree.isRowHidden(item.row(), item.parent().index())
    dialog.show_hidden.setChecked(False)
    assert dialog.tree.isRowHidden(item.row(), item.parent().index())
    assert dialog.summary.text().startswith("3 of 4 files will be read.")

    # Read these: the shown ones are read in the background.
    _scan_after(qtbot, window, dialog.accept)
    qtbot.waitUntil(lambda: len(_read(window)) == 3, timeout=10_000)
    assert _read(window) == INSIDE - {f"{PACK}/UI pack/button 1.png"}

    # It opens again with the choice made.
    dialog = _dialog(qtbot, window)
    assert dialog.hidden() == {"UI pack/button 1.png"}
    dialog.reject()  # Cancel changes nothing
    assert _read(window) == INSIDE - {f"{PACK}/UI pack/button 1.png"}


def test_an_archives_commands_and_hiding_one_file(qtbot: QtBot, window: MainWindow) -> None:
    page = _page(qtbot, window, "UI pack.zip")
    actions = _file_actions(page, PACK)
    assert {CHOOSE, READ_ALL, STOP} <= set(actions)

    _scan_after(qtbot, window, actions[READ_ALL].trigger)  # type: ignore[attr-defined]
    qtbot.waitUntil(lambda: _read(window) == INSIDE, timeout=10_000)
    cover = _item(window, "cover.png")

    # Hide inside its archive, from the cover's own page: its item hides at once.
    page = _page(qtbot, window, "cover.png")
    hide = _file_actions(page, f"{PACK}/UI pack/cover.png")
    assert HIDE in hide
    with qtbot.waitSignal(window.archives.changed, timeout=5000) as said:
        hide[HIDE].trigger()  # type: ignore[attr-defined]
    assert said.args[0].startswith("Hid cover.png inside UI pack.zip")
    assert _read(window) == INSIDE - {f"{PACK}/UI pack/cover.png"}
    # The archive is still read whole: hiding one doesn't stop the others.
    with qtbot.waitSignal(window.scans.finished, timeout=20_000):
        window.scan_now()
    assert _read(window) == INSIDE - {f"{PACK}/UI pack/cover.png"}

    # Stop reading inside: all of them hide at once; Read everything inside brings them back.
    page = _page(qtbot, window, "UI pack.zip")
    actions = _file_actions(page, PACK)
    with qtbot.waitSignal(window.archives.changed, timeout=5000):
        actions[STOP].trigger()  # type: ignore[attr-defined]
    assert _read(window) == set()
    _scan_after(qtbot, window, _file_actions(page, PACK)[READ_ALL].trigger)  # type: ignore[attr-defined]
    qtbot.waitUntil(lambda: _read(window) == INSIDE, timeout=10_000)
    assert _item(window, "cover.png") == cover  # the same item, back with its tags


def test_hiding_a_copy_in_duplicates(qtbot: QtBot, window: MainWindow) -> None:
    _scan_after(qtbot, window, lambda: window.archives.read_everything(_id(window, PACK)))
    qtbot.waitUntil(lambda: _read(window) == INSIDE, timeout=10_000)
    window.navigation.select(NavTarget("dedupe", label="Dedupe"))
    page = window.stack.currentWidget()
    assert isinstance(page, DedupePage)
    qtbot.waitUntil(
        lambda: any(f.archive for g in page.groups or [] for f in g.files), timeout=10_000
    )
    group = next(g for g in page.groups or [] if any(f.archive for f in g.files))
    member = next(f for f in group.files if f.archive)
    menu = page.file_menu(member)
    assert menu is not None
    actions = {a.text(): a for a in menu.actions()}
    with qtbot.waitSignal(window.archives.changed, timeout=5000):
        actions[HIDE].trigger()
    assert member.relpath not in _read(window)


def test_reading_a_folders_archives(qtbot: QtBot, window: MainWindow) -> None:
    page = _page(qtbot, window, "Aurora Studio (")
    actions = _file_actions(page, "Aurora Studio")
    assert READ_FOLDER in actions
    _scan_after(qtbot, window, actions[READ_FOLDER].trigger)  # type: ignore[attr-defined]
    qtbot.waitUntil(lambda: _read(window) == INSIDE, timeout=10_000)
    root = _session(window).keep.config.roots[0]
    assert root.archives == ["Aurora Studio/**"]

    # Keep configuration shows it, and editing the box saves it.
    config = window.configure_keep()
    qtbot.waitUntil(lambda: config.archives.toPlainText() == "Aurora Studio/**", timeout=5000)
    config.archives.setPlainText("")
    with qtbot.waitSignal(config.changed, timeout=5000) as said:
        QApplication.sendEvent(config.archives, QFocusEvent(QEvent.Type.FocusOut))
    assert "Scan it to update its files" in said.args[0]
    qtbot.waitUntil(lambda: _session(window).keep.config.roots[0].archives == [], timeout=5000)
    config.close()


def test_a_search_results_archive_menu(qtbot: QtBot, window: MainWindow) -> None:
    window.navigation.select(NavTarget("view", key="Assets", label="Assets"))
    search = window.stack.currentWidget()
    assert isinstance(search, SearchPage)
    qtbot.waitUntil(lambda: search.model.rowCount() > 0, timeout=5000)

    def listed() -> dict[str, SearchHit]:
        hits = [search.model.hit(r) for r in range(search.model.rowCount())]
        return {h.title: h for h in hits if h is not None}

    qtbot.waitUntil(lambda: {"UI pack.zip", "Aileron-Regular.ttf"} <= set(listed()), timeout=5000)
    hits = listed()

    def inside(title: str) -> QMenu | None:
        menu = search.item_menu(hits[title])
        return next(
            (m for m in menu.findChildren(QMenu) if m.title() == "Inside the archive"), None
        )

    assert inside("Aileron-Regular.ttf") is None  # a font: never an archive
    submenu = inside("UI pack.zip")
    assert isinstance(submenu, InsideArchiveMenu)
    submenu.fill()
    qtbot.waitUntil(
        lambda: [a.text() for a in submenu.actions()] == [CHOOSE, READ_ALL, STOP], timeout=5000
    )


def test_show_all_hide_all_and_thumbnails(qtbot: QtBot, window: MainWindow) -> None:
    dialog = _dialog(qtbot, window)
    assert not dialog.show_thumbnails.isChecked()  # off unless chosen

    # Hide all, then show the one to read: the fewest clicks for a few files.
    dialog.hide_all.click()
    assert dialog.shown() == set()
    dialog._clicked(_row(dialog, "cover.png").siblingAtColumn(HIDE_COLUMN))
    assert dialog.shown() == {"UI pack/cover.png"}
    assert dialog.summary.text().startswith("1 of 4 files will be read.")
    dialog.show_all.click()
    assert dialog.hidden() == set()

    # Show thumbnails draws each file, and the keep remembers it.
    with qtbot.waitSignal(dialog.thumbnails_drawn, timeout=10_000):
        dialog.show_thumbnails.setChecked(True)
    cover = dialog.model.itemFromIndex(_row(dialog, "cover.png"))
    assert not cover.icon().isNull()
    assert dialog.tree.iconSize().width() > 16
    dialog.show_thumbnails.setChecked(False)
    assert cover.icon().isNull()
    dialog.show_thumbnails.setChecked(True)
    assert not cover.icon().isNull()  # drawn already
    dialog.reject()
    qtbot.waitUntil(lambda: window.archives.dialog is None, timeout=5000)

    reopened = _dialog(qtbot, window)
    assert reopened.show_thumbnails.isChecked()
    again = reopened.model.itemFromIndex(_row(reopened, "cover.png"))
    qtbot.waitUntil(lambda: not again.icon().isNull(), timeout=10_000)
    state = load_ui_state(_session(window).keep.ui_state_path)
    assert state["archive_thumbnails"] is True
