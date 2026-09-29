"""The grid layout with thumbnails loaded in the background (#77)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt, QThreadPool
from PySide6.QtGui import QDropEvent, QWheelEvent
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.ui_state import load_ui_state
from tagalot.ui.dnd import tags_mime
from tagalot.ui.main_window import MainWindow
from tagalot.ui.search_view import SearchPage
from tagalot.ui.thumbnails import size_presets, zoomed
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


@pytest.fixture
def keep_dir(tmp_path: Path) -> Path:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path: Path = module.make_demo(tmp_path)
    return path


@pytest.fixture
def session(keep_dir: Path) -> Iterator[KeepSession]:
    with KeepSession.open(keep_dir, Settings()) as session:
        yield session


def _window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()
    qtbot.waitUntil(lambda: _page(window).status.text() == "9 items", timeout=5000)
    return window


def _close(window: MainWindow) -> None:
    window.thumbnails.clear()
    window.thumbnails.wait()


def _page(window: MainWindow) -> SearchPage:
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    return page


def _grid_page(qtbot: QtBot, window: MainWindow) -> SearchPage:
    page = _page(window)
    page.grid_button.click()
    assert page.results.currentWidget() is page.grid
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    return page


def _row(page: SearchPage, title: str) -> int:
    for row in range(page.model.rowCount()):
        hit = page.model.hit(row)
        if hit is not None and hit.title == title:
            return row
    raise AssertionError(title)


# --- sizes ---


def test_size_presets_stop_at_the_maximum() -> None:
    assert size_presets(256) == [("Small", 64), ("Medium", 128), ("Largest", 256)]
    assert size_presets(1024) == [("Small", 64), ("Medium", 128), ("Large", 256), ("Largest", 1024)]
    assert size_presets(100) == [("Small", 64), ("Largest", 100)]


def test_zoom_steps() -> None:
    assert zoomed(128, 1, 256) == 160
    assert zoomed(128, -1, 256) == 96
    assert zoomed(256, 1, 256) == 256  # already the largest
    assert zoomed(48, -1, 256) == 48
    assert zoomed(200, 1, 1024) == 256  # from between steps


# --- the layout toggle ---


def test_the_toggle_switches_and_is_remembered(
    qtbot: QtBot, session: KeepSession, keep_dir: Path
) -> None:
    window = _window(qtbot, session)
    page = _page(window)
    assert page.results.currentWidget() is page.table
    assert page.list_button.isChecked()
    with qtbot.waitSignal(page.layout_changed):
        page.grid_button.click()
    assert page.results.currentWidget() is page.grid
    assert page.grid_button.isChecked()
    assert load_ui_state(session.keep.ui_state_path)["layouts"] == {"search:": "grid"}
    _close(window)

    again = _window(qtbot, session)
    assert _page(again).results.currentWidget() is _page(again).grid
    _close(again)


def test_switching_keeps_the_selection(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    page = _grid_page(qtbot, window)
    row = _row(page, "sunset.jpg")
    center = page.grid.visualRect(page.model.index(row, 0)).center()
    QTest.mouseClick(page.grid.viewport(), Qt.MouseButton.LeftButton, pos=center)
    selected = [i.row() for i in page.table.selectionModel().selectedRows()]
    assert selected == [row]  # whole rows, so the list shows them selected too
    ids: list[list[int]] = []
    page.selected_entity_ids(ids.append)
    assert ids == [[page.model.hit(row).id]]  # type: ignore[union-attr]
    page.list_button.click()
    assert [i.row() for i in page.table.selectionModel().selectedRows()] == [row]
    _close(window)


# --- thumbnails ---


def test_thumbnails_and_icons_load_in_the_background(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    page = _grid_page(qtbot, window)
    loader = window.thumbnails
    photo = page.model.hit(_row(page, "glacier.jpg"))
    text = page.model.hit(_row(page, "notes.txt"))
    assert photo is not None
    assert text is not None
    page.grid.grab()  # painting asks for what's on screen
    qtbot.waitUntil(lambda: loader.get(photo.id) is not None, timeout=10_000)
    qtbot.waitUntil(lambda: loader.get(text.id) is not None, timeout=10_000)
    image = loader.get(photo.id).image  # type: ignore[union-attr]
    assert image is not None
    assert max(image.width(), image.height()) <= session.thumbnail_max
    assert loader.get(text.id).image is None  # type: ignore[union-attr]
    assert loader.get(text.id).icon == "file"  # type: ignore[union-attr]
    assert not page.grid.grab().isNull()  # paints pictures and icons
    loader.clear()
    assert loader.get(photo.id) is None  # asked for again
    _close(window)


# --- card lines ---


def test_card_lines_are_chosen_and_remembered(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    page = _grid_page(qtbot, window)
    assert page.card_lines() == {"generic.file": []}  # the generic theme shows titles only
    labels = [a.text() for a in page.grid_menu().actions()]
    assert labels[:5] == ["Card lines", "Extension", "Folder", "Size (bytes)", "Modified"]
    height = page.grid.gridSize().height()

    with qtbot.waitSignal(page.card_lines_changed):
        page.set_card_lines(["size", "extension"])
    assert page.card_lines() == {
        "generic.file": [("size", "Size (bytes)"), ("extension", "Extension")]
    }
    assert page.grid.gridSize().height() > height  # room for two more lines
    assert set(page.model.extra_fields) == {"size", "extension"}
    qtbot.waitUntil(lambda: (row := page.model.row(0, load=False)) is not None and "size" in row[1])
    state = load_ui_state(session.keep.ui_state_path)
    assert state["card_lines"] == {"search:": ["size", "extension"]}

    menu = page.grid_menu()
    by_label = {a.text(): a for a in menu.actions()}
    assert by_label["Size (bytes)"].isChecked()
    by_label["Size (bytes)"].toggle()  # unchecking removes that line
    assert page.card_lines() == {"generic.file": [("extension", "Extension")]}
    by_label["Use the theme's card lines"].trigger()
    assert page.card_lines() == {"generic.file": []}
    assert "search:" not in load_ui_state(session.keep.ui_state_path)["card_lines"]
    _close(window)


# --- sizes in the window ---


def test_view_menu_sizes_and_zoom(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    page = _grid_page(qtbot, window)
    assert window.thumbnail_size == 128  # the theme's default
    assert sorted(window.size_actions) == [64, 128, 256]
    assert window.size_actions[128].isChecked()
    window.size_actions[64].trigger()
    assert page.grid.thumbnail_size == 64
    window.zoom_in_action.trigger()
    assert page.grid.thumbnail_size == 80
    assert not any(a.isChecked() for a in window.size_actions.values())  # between presets
    assert load_ui_state(session.keep.ui_state_path)["thumbnail_size"] == 80

    with qtbot.waitSignal(page.zoom_requested) as blocker:
        wheel = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        page.grid.wheelEvent(wheel)
    assert blocker.args == [1]
    assert page.grid.thumbnail_size == 96
    _close(window)


def test_a_keep_can_raise_the_maximum(qtbot: QtBot, keep_dir: Path) -> None:
    toml = keep_dir / "keep.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + "\n[thumbnails]\nmax_size = 512\n", encoding="utf-8"
    )
    with KeepSession.open(keep_dir, Settings()) as session:
        assert session.thumbnail_max == 512
        assert session.thumbnails.size == 512
        window = _window(qtbot, session)
        assert sorted(window.size_actions) == [64, 128, 256, 512]
        _close(window)


# --- dropping tags ---


def test_tags_dropped_on_a_card(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    page = _grid_page(qtbot, window)
    row = _row(page, "geyser.jpg")
    center = page.grid.visualRect(page.model.index(row, 0)).center()
    mime = tags_mime([1])  # the event only points to it: keep it alive
    event = QDropEvent(
        QPointF(center),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    with qtbot.waitSignal(page.tags_dropped) as blocker:
        page.grid.dropEvent(event)
    assert blocker.args == [[page.model.hit(row).id], [1]]  # type: ignore[union-attr]
    _close(window)
