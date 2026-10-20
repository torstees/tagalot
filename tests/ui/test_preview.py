"""The preview of the selected item (#90), under the navigation pane since #493."""

import pytest
from PySide6.QtCore import QItemSelectionModel
from pytestqt.qtbot import QtBot

from tagalot.core.ui_state import load_ui_state
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import PREVIEW_HEIGHT, MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.preview import HINT, PreviewStrip
from tagalot.ui.search_view import SearchPage
from tests.ui.test_contents_search import _open, session, window
from tests.ui.test_within import _search

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

IMAGES = NavTarget("view", key="Images", label="Images")


def _select(page: SearchPage, titles: list[str]) -> None:
    selection = page.table.selectionModel()
    selection.clearSelection()
    for row in range(page.model.rowCount()):
        hit = page.model.hit(row)
        if hit is not None and hit.title in titles:
            selection.select(
                page.model.index(row, 0),
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )


def test_one_selected_item_is_previewed(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, IMAGES, "9 items")
    strip = window.preview
    assert strip.isVisible()
    assert strip.title.text() == HINT
    assert not strip.thumbnail.isVisible()  # nothing selected: the words at the top
    assert window.left.indexOf(strip) == 1  # under the navigation, not the results
    assert page.findChild(PreviewStrip) is None
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    _select(page, ["forest.png"])
    qtbot.waitUntil(lambda: strip.title.text() == "forest.png · Image", timeout=5000)
    assert "1600 \u00d7 900" in strip.facts.text()
    assert "Aurora Studio" in strip.facts.text()
    assert strip.file.full_text == "Asset files \u203a Aurora Studio/Backgrounds/forest.png"
    qtbot.waitUntil(lambda: not strip.thumbnail.pixmap().isNull(), timeout=10_000)

    _select(page, ["forest.png", "gem.png"])
    qtbot.waitUntil(lambda: strip.title.text() == "2 items selected", timeout=5000)
    assert strip.facts.text() == ""
    _select(page, [])
    qtbot.waitUntil(lambda: strip.title.text() == HINT, timeout=5000)


def test_double_click_the_strip_opens_the_page(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, IMAGES, "9 items")
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    _select(page, ["gem.png"])
    qtbot.waitUntil(lambda: window.preview.title.text().startswith("gem.png"), timeout=5000)
    window.preview.open_requested.emit(window.preview.entity_id)
    detail = window.stack.currentWidget()
    assert isinstance(detail, DetailPage)


def test_search_alls_sections_are_previewed(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, NavTarget("search", label="Search all"), "14 items")
    section = next(s for s in page.groups.sections if s.group.label == "Fonts")
    # Through the selection model: selectRow() follows the keyboard modifiers Qt thinks are
    # held, which an earlier test's key presses can leave behind.
    section.table.selectionModel().select(
        section.model.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )

    def previewed() -> None:
        assert window.preview.title.text() == "Aileron-Regular.ttf · Font"

    qtbot.waitUntil(previewed, timeout=10_000)


def test_view_preview_hides_it_and_is_remembered(qtbot: QtBot, window: MainWindow) -> None:
    session = window.session
    assert session is not None
    page = _search(qtbot, window, IMAGES, "9 items")
    window.preview_action.setChecked(False)
    assert window.preview.isHidden()
    assert load_ui_state(session.keep.ui_state_path)["preview"] is False
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    _select(page, ["gem.png"])
    window.preview_action.setChecked(True)  # shows what is selected now
    qtbot.waitUntil(lambda: window.preview.title.text().startswith("gem.png"), timeout=5000)


def test_a_detail_pages_contents_are_previewed(qtbot: QtBot, window: MainWindow) -> None:
    session = window.session
    assert session is not None
    detail = _open(qtbot, window, session, "Kenji Sato")
    contents = detail.contents
    assert contents is not None
    qtbot.waitUntil(lambda: contents.model.hit(0) is not None, timeout=5000)
    first = contents.model.hit(0)
    assert first is not None
    _select(contents, [first.title])
    qtbot.waitUntil(lambda: window.preview.title.text().startswith(first.title), timeout=5000)


def test_the_previews_height_is_remembered(qtbot: QtBot, window: MainWindow) -> None:
    session = window.session
    assert session is not None
    window.resize(1200, 800)
    qtbot.waitUntil(lambda: window.left.sizes()[1] == PREVIEW_HEIGHT, timeout=5000)
    total = sum(window.left.sizes())
    window.left.setSizes([total - 250, 250])
    window._preview_resized()
    assert load_ui_state(session.keep.ui_state_path)["preview_height"] == 250
