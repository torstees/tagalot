"""Opening an item's detail page and tagging from it (#86)."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QWidget
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.tags import entity_tags
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tests.ui.test_grid import _close, _page, _row, _window, keep_dir, session

pytestmark = pytest.mark.gui

__all__ = ["keep_dir", "session"]  # fixtures


def _open(qtbot: QtBot, window: MainWindow, title: str) -> DetailPage:
    page = _page(window)
    row = _row(page, title)
    index = page.model.index(row, 0)
    page.table.scrollTo(index)
    rect = page.table.visualRect(index)
    # A real double-click is a click and then a double-click event on the same spot.
    QTest.mouseClick(page.table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    QTest.mouseDClick(page.table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    detail = window.stack.currentWidget()
    assert isinstance(detail, DetailPage)
    qtbot.waitUntil(lambda: detail.detail is not None, timeout=5000)
    return detail


def _texts(detail: DetailPage, section: str) -> list[str]:
    box = detail.findChild(QWidget, f"section_{section}")
    assert box is not None, section
    return [label.text() for label in box.findChildren(QLabel)]


def test_double_click_opens_the_detail_page(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    detail = _open(qtbot, window, "glacier.jpg")
    assert detail.title.text() == "glacier.jpg"
    assert detail.type_label.text() == "File"
    fields = _texts(detail, "fields")
    assert "Extension:" in fields
    assert ".jpg" in fields
    assert "Photos/Iceland" in fields
    files = " ".join(_texts(detail, "role"))
    assert "Photos/Iceland/glacier.jpg" in files
    assert "Demo files" in files  # the root's name
    qtbot.waitUntil(
        lambda: detail.thumbnail.pixmap() is not None and not detail.thumbnail.pixmap().isNull(),
        timeout=10_000,
    )
    _close(window)


def test_the_same_item_reuses_its_page(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    first = _open(qtbot, window, "glacier.jpg")
    window.navigation.select(NavTarget("search", label="Search all"))  # already highlighted
    assert window.stack.currentWidget() is _page(window)
    assert _open(qtbot, window, "glacier.jpg") is first
    _close(window)


def test_tagging_from_a_detail_page(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    detail = _open(qtbot, window, "notes.txt")
    tree = session.tag_cache.get()
    sky = tree.find_child(tree.find_child(None, "Topics"), "Sky")
    assert sky is not None
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.tag_selection([sky])
    with session.reader.connect() as conn:
        assert sky in entity_tags(conn, [detail.entity_id])[detail.entity_id]
    label = window.tag_panel.selection_label
    qtbot.waitUntil(lambda: label.text().startswith("1 item selected"), timeout=5000)
    _close(window)


def test_a_missing_entity(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    window.open_entity(999_999)
    detail = window.stack.currentWidget()
    assert isinstance(detail, DetailPage)
    qtbot.waitUntil(lambda: detail.title.text() == "This item no longer exists", timeout=5000)
    ids: list[list[int]] = []
    detail.selected_entity_ids(ids.append)
    assert ids == [[]]
    _close(window)


def test_clicking_search_all_leaves_a_detail_page(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    search = _page(window)
    _open(qtbot, window, "glacier.jpg")
    nav = window.navigation
    assert not nav.currentIndex().isValid()  # a detail page isn't in the navigation
    target = NavTarget("search", label="Search all")
    index = nav.index_of(target)
    assert index.isValid()
    rect = nav.visualRect(index)
    with qtbot.waitSignal(nav.navigate):
        QTest.mouseClick(nav.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert window.stack.currentWidget() is search
    assert nav.currentIndex() == index  # highlighted again
    _close(window)
