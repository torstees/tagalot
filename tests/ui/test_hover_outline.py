"""Results lists show the item under the mouse with an outline, not a fill (#473)."""

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt, QThreadPool
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QAbstractItemView, QApplication, QStyle, QStyleOptionViewItem
from pytestqt.qtbot import QtBot

from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.ui.hover import NoHoverFill, _without_hover
from tagalot.ui.search_view import SearchPage
from tests.ui.test_grouped_search import ALBUM, session  # noqa: F401  (the keep)

pytestmark = pytest.mark.gui


@pytest.fixture
def albums(qtbot: QtBot, session: KeepSession) -> SearchPage:  # noqa: F811
    page = SearchPage(session, "Albums", SearchSpec(types=(ALBUM,)), pool=QThreadPool())
    qtbot.addWidget(page)
    page.resize(900, 700)
    page.show()
    qtbot.waitUntil(lambda: page.status.text() == "11 items", timeout=5000)
    return page


def _view(page: SearchPage, layout: str) -> QAbstractItemView:
    page.set_layout(layout)
    return {"list": page.table, "grid": page.grid, "tree": page.tree}[layout]


def _move(view: QAbstractItemView, row: int) -> None:
    point = QPointF(view.visualRect(view.model().index(row, 0)).center())
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        point,
        QPointF(view.viewport().mapToGlobal(point.toPoint())),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(view.viewport(), event)


@pytest.mark.parametrize("layout", ["list", "grid", "tree"])
def test_the_item_under_the_mouse_is_outlined(
    qtbot: QtBot, albums: SearchPage, layout: str
) -> None:
    view = _view(albums, layout)
    qtbot.waitUntil(lambda: view.isVisible() and view.model().rowCount() == 11, timeout=5000)
    hover = view.hover  # type: ignore[attr-defined]
    assert isinstance(view.style(), NoHoverFill)  # the style's own hover fill isn't drawn
    assert not hover.frame.isVisible()

    _move(view, 2)
    item = view.visualRect(view.model().index(2, 0))
    frame = hover.frame.geometry()
    assert hover.frame.isVisible()
    if layout == "grid":  # the card
        assert item.contains(frame)
        assert frame.width() > item.width() - 4
    else:  # the whole row
        assert (frame.top(), frame.height()) == (item.top(), item.height())
        assert frame.width() == view.viewport().width()

    _move(view, 5)  # it follows the mouse
    assert hover.frame.geometry().top() == hover.rect_for(view.model().index(5, 0)).top()

    QApplication.sendEvent(view.viewport(), QEvent(QEvent.Type.Leave))
    assert not hover.frame.isVisible()


def test_hover_is_left_out_of_the_items_drawing() -> None:
    option = QStyleOptionViewItem()
    option.state = QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_MouseOver
    plain = _without_hover(option)
    assert not plain.state & QStyle.StateFlag.State_MouseOver
    assert plain.state & QStyle.StateFlag.State_Enabled
    assert option.state & QStyle.StateFlag.State_MouseOver  # a copy: the view's own is kept
