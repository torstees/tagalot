"""The preview strip under search results (#90)."""

import pytest
from PySide6.QtCore import QItemSelectionModel
from pytestqt.qtbot import QtBot

from tagalot.core.ui_state import load_ui_state
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.preview import HINT
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
    strip = page.preview
    assert strip.isVisible()
    assert strip.title.text() == HINT
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    _select(page, ["forest.png"])
    qtbot.waitUntil(lambda: strip.title.text() == "forest.png · Image", timeout=5000)
    assert "1600 \u00d7 900" in strip.facts.text()
    assert "Aurora Studio" in strip.facts.text()
    assert strip.file.text() == "Asset files \u203a Aurora Studio/Backgrounds/forest.png"
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
    qtbot.waitUntil(lambda: page.preview.title.text().startswith("gem.png"), timeout=5000)
    with qtbot.waitSignal(page.open_requested):
        page.preview.open_requested.emit(page.preview.entity_id)
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
        assert page.preview.title.text() == "Aileron-Regular.ttf · Font"

    qtbot.waitUntil(previewed, timeout=10_000)


def test_view_preview_hides_it_everywhere_and_is_remembered(
    qtbot: QtBot, window: MainWindow
) -> None:
    session = window.session
    assert session is not None
    images = _search(qtbot, window, IMAGES, "9 items")
    window.preview_action.setChecked(False)
    assert images.preview.isHidden()
    assert load_ui_state(session.keep.ui_state_path)["preview"] is False
    fonts = _search(qtbot, window, NavTarget("view", key="Fonts", label="Fonts"), "1 item")
    assert fonts.preview.isHidden()  # pages made later start hidden too
    window.preview_action.setChecked(True)
    assert not images.preview.isHidden()


def test_a_detail_pages_contents_have_a_preview(qtbot: QtBot, window: MainWindow) -> None:
    session = window.session
    assert session is not None
    detail = _open(qtbot, window, session, "Kenji Sato")
    assert detail.contents is not None
    assert detail.contents.preview.isVisibleTo(detail)
