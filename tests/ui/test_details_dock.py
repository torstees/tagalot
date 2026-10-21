"""The Details tab beside Tags: an item page's sections, or a search's selected item (#468)."""

import json

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt, QThreadPool
from PySide6.QtWidgets import QFormLayout, QLabel, QTabBar, QWidget
from pytestqt.qtbot import QtBot

from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.details_dock import NOTHING_SELECTED, several_selected
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.workers import ScanController
from tests.ui.test_detail_page import _open
from tests.ui.test_grid import _close, _page, _row, _window, keep_dir, session

pytestmark = pytest.mark.gui

__all__ = ["keep_dir", "session"]  # fixtures


def _shown(window: MainWindow, name: str) -> QWidget | None:
    """A section in the Details tab, if it shows one."""
    panel = window.details_dock.panel
    return panel.findChild(QWidget, name) if panel is not None else None


def _select(window: MainWindow, *titles: str) -> None:
    page = _page(window)
    selection = page.table.selectionModel()
    selection.clearSelection()
    for title in titles:
        selection.select(
            page.model.index(_row(page, title), 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )


def _side_tab(window: MainWindow) -> str:
    """The tab shown of the side panel's two, Tags and Details."""
    [bar] = [
        b
        for b in window.findChildren(QTabBar)
        if sorted(b.tabText(i) for i in range(b.count())) == ["Details", "Tags"]
    ]
    return bar.tabText(bar.currentIndex())


def _message(window: MainWindow) -> str | None:
    dock = window.details_dock
    return dock.message.text() if dock.stack.currentWidget() is dock.message else None


def test_an_item_pages_sections_are_in_the_details_tab(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    detail = _open(qtbot, window, "glacier.jpg")
    qtbot.waitUntil(lambda: window.details_dock.panel is detail.details, timeout=5000)
    qtbot.waitUntil(lambda: _shown(window, "section_fields") is not None, timeout=5000)
    assert _shown(window, "section_extra") is not None
    assert _shown(window, "section_role") is not None  # its files
    # The middle keeps the header (and searches), not the sections.
    assert detail.splitter.findChild(QWidget, "section_fields") is None
    assert detail.title.text() == "glacier.jpg"
    # Not named again in the tab: the page shows it.
    assert not detail.details_title.isVisible()
    # Labels above their values: the tab is narrow.
    fields = _shown(window, "section_fields")
    assert fields is not None
    forms = fields.findChildren(QFormLayout)
    assert forms
    assert all(f.rowWrapPolicy() == QFormLayout.RowWrapPolicy.WrapAllRows for f in forms)

    window.back()  # Search all, where glacier.jpg is still selected
    qtbot.waitUntil(lambda: window.details_dock.panel is not detail.details, timeout=5000)
    assert detail.details.parent() is detail  # back with its page, hidden
    assert not detail.details.isVisible()
    _close(window)


def test_a_searchs_selected_item_is_in_the_details_tab(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    qtbot.waitUntil(lambda: _message(window) == NOTHING_SELECTED, timeout=5000)
    _select(window, "glacier.jpg")
    qtbot.waitUntil(lambda: _shown(window, "section_fields") is not None, timeout=5000)
    title = window.details_dock.findChild(QLabel, "details_title")
    assert title is not None
    assert title.isVisible()  # named, since its page isn't shown
    assert "glacier.jpg" in title.text()

    _select(window, "glacier.jpg", "sunset.jpg")
    qtbot.waitUntil(lambda: _message(window) == several_selected(2), timeout=5000)
    _select(window)
    qtbot.waitUntil(lambda: _message(window) == NOTHING_SELECTED, timeout=5000)

    # Its name opens its page.
    selected = window._selected_detail
    assert selected is not None
    selected.details_title.linkActivated.emit("open")
    qtbot.waitUntil(
        lambda: (
            isinstance(page := window.stack.currentWidget(), DetailPage)
            and page.title.text() == "glacier.jpg"
        ),
        timeout=5000,
    )
    _close(window)


def test_editing_the_selected_items_details(qtbot: QtBot, session: KeepSession) -> None:
    """The selected item's details are edited as on its page: an Extra field, here."""
    window = _window(qtbot, session)
    _select(window, "glacier.jpg")
    qtbot.waitUntil(lambda: _shown(window, "section_extra") is not None, timeout=5000)
    selected = window._selected_detail
    assert selected is not None
    selected.extra_name.setText("Mood")
    selected.extra_value.setText("Cold")
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        selected.extra_value.returnPressed.emit()
    with session.reader.connect() as conn:
        extra = (
            conn.execute(Entity.__table__.select().where(Entity.title == "glacier.jpg"))
            .one()
            ._mapping["extra"]
        )
    assert extra == {"Mood": "Cold"}
    _close(window)


def test_the_tab_chosen_and_the_width_are_remembered(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    assert _side_tab(window) == "Tags"  # first, by default
    window.details_dock.raise_()
    window.tabifiedDockWidgetActivated.emit(window.details_dock)  # as a click on its tab
    window.resize(1500, 700)  # room beside the middle's minimum width
    qtbot.wait(50)
    window.resizeDocks([window.details_dock], [330], Qt.Orientation.Horizontal)
    qtbot.waitUntil(lambda: window.details_dock.width() == 330, timeout=5000)
    window._side_resized()
    saved = json.loads(session.keep.ui_state_path.read_text(encoding="utf-8"))
    assert saved["side_tab"] == "details_dock"
    assert saved["side_width"] == 330
    _close(window)
    window.close()

    again = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(again)
    again.resize(1500, 700)
    again.show()
    assert _side_tab(again) == "Details"
    qtbot.waitUntil(lambda: again.details_dock.width() == 330, timeout=5000)
    _close(again)


def test_the_tag_manager_hides_the_side_panel(qtbot: QtBot, session: KeepSession) -> None:
    """It has its own tree and details (#508); other pages get the panel back as it was."""
    window = _window(qtbot, session)
    window.details_dock.raise_()
    window.tabifiedDockWidgetActivated.emit(window.details_dock)  # Details chosen
    assert _side_tab(window) == "Details"
    window.navigation.select(NavTarget("tags", label="Tag manager"))
    assert not window.tags_dock.toggleViewAction().isChecked()
    assert not window.details_dock.toggleViewAction().isChecked()
    window.navigation.select(NavTarget("search", label="Search all"))
    assert window.tags_dock.toggleViewAction().isChecked()
    assert window.details_dock.toggleViewAction().isChecked()
    assert _side_tab(window) == "Details"  # the tab chosen before

    window.tags_dock.toggleViewAction().trigger()  # the user closed Tags
    window.navigation.select(NavTarget("tags", label="Tag manager"))
    window.navigation.select(NavTarget("search", label="Search all"))
    assert not window.tags_dock.toggleViewAction().isChecked()  # still closed
    assert window.details_dock.toggleViewAction().isChecked()
    _close(window)
