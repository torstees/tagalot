"""Field filters in the filter bar: "+ Field", the popup, and chips (#94)."""

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from tagalot.core.search_spec import ChoiceFilter, RangeFilter, TextFilter, TextMatch
from tagalot.ui.field_filters import FieldFilterPopup, FilterField, describe
from tagalot.ui.filter_bar import FilterBar
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tests.ui.test_contents_search import session, window
from tests.ui.test_within import _search

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

IMAGES = NavTarget("view", key="Images", label="Images")


def _popup(qtbot: QtBot, page: SearchPage, name: str) -> FieldFilterPopup:
    page.filter_bar.edit_field(name)
    popup = page.filter_bar.popup
    assert popup is not None
    qtbot.addWidget(popup)
    return popup


def _choices(popup: FieldFilterPopup) -> list[str]:
    assert popup.values is not None
    return [popup.values.item(i).text() for i in range(popup.values.count())]


def _chip_texts(page: SearchPage) -> list[str]:
    return [chip.label.text() for chip in page.filter_bar._field_chips.values()]


# --- reading filters ---


@pytest.mark.parametrize(
    ("filter_", "text"),
    [
        (ChoiceFilter("extension", (".png",)), "Extension: .png"),
        (ChoiceFilter("extension", (".a", ".b", ".c", ".d", ".e")), "Extension: .a, .b, .c +2"),
        (RangeFilter("width", 500, 1920), "Width: 500\u20131920"),
        (RangeFilter("width", 500, None), "Width \u2265 500"),
        (RangeFilter("width", None, 800), "Width \u2264 800"),
        (TextFilter("folder", "ice"), 'Folder contains "ice"'),
        (TextFilter("folder", "Aur", TextMatch.STARTS_WITH), 'Folder starts with "Aur"'),
    ],
)
def test_describe(filter_: object, text: str) -> None:
    label = {"extension": "Extension", "width": "Width", "folder": "Folder"}
    assert describe(filter_, label[filter_.field]) == text  # type: ignore[arg-type, attr-defined]


# --- on a search page ---


def test_the_field_menu_lists_fields_every_type_has(qtbot: QtBot, window: MainWindow) -> None:
    images = _search(qtbot, window, IMAGES, "9 items")
    labels = [a.text() for a in images.filter_bar.field_menu.actions()]
    assert labels == [
        "Artist",
        "Extension",
        "Folder",
        "Size (bytes)",
        "Modified",
        "Width",
        "Height",
    ]
    everything = _search(qtbot, window, NavTarget("search", label="Search all"), "14 items")
    assert not everything.filter_bar.field_button.isEnabled()  # artists have no fields


def test_a_choice_filter(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, IMAGES, "9 items")
    popup = _popup(qtbot, page, "extension")
    qtbot.waitUntil(lambda: _choices(popup) == [".png  (6)", ".jpg  (3)"], timeout=5000)
    assert popup.values is not None
    popup.values.item(1).setCheckState(Qt.CheckState.Checked)
    popup._apply()
    qtbot.waitUntil(lambda: page.status.text() == "3 items", timeout=5000)
    assert _chip_texts(page) == ["Extension: .jpg"]

    # Editing it again shows what's chosen, and the other value's count.
    again = _popup(qtbot, page, "extension")
    qtbot.waitUntil(lambda: len(_choices(again)) == 2, timeout=5000)
    assert again.values is not None
    checked = [
        again.values.item(i).text()
        for i in range(again.values.count())
        if again.values.item(i).checkState() == Qt.CheckState.Checked
    ]
    assert checked == [".jpg  (3)"]
    again._clear()
    qtbot.waitUntil(lambda: page.status.text() == "9 items", timeout=5000)
    assert _chip_texts(page) == []


def test_a_range_filter(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, IMAGES, "9 items")
    popup = _popup(qtbot, page, "width")
    assert popup.low is not None
    popup.low.setText("five hundred")
    popup._apply()
    assert popup.isVisible()  # stays open with the reason
    assert "whole number" in popup.error.text()
    popup.low.setText("500")
    popup._apply()
    qtbot.waitUntil(lambda: page.status.text() == "6 items", timeout=5000)
    assert _chip_texts(page) == ["Width \u2265 500"]


def test_a_text_filter(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, IMAGES, "9 items")
    popup = _popup(qtbot, page, "folder")
    assert popup.text is not None
    popup.text.setText("icons")
    popup._apply()
    qtbot.waitUntil(lambda: page.status.text() == "2 items", timeout=5000)
    hits = [page.model.hit(r) for r in range(page.model.rowCount())]
    assert sorted(h.title for h in hits if h) == ["gem.png", "heart.png"]


def test_clear_all_removes_field_filters(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, IMAGES, "9 items")
    page.filter_bar.set_field_filter("width", RangeFilter("width", 500, None))
    qtbot.waitUntil(lambda: page.status.text() == "6 items", timeout=5000)
    page.filter_bar.clear()
    qtbot.waitUntil(lambda: page.status.text() == "9 items", timeout=5000)
    assert _chip_texts(page) == []


def test_a_filter_on_a_field_the_scope_lacks_is_kept_but_not_applied(qtbot: QtBot) -> None:
    bar = FilterBar()
    qtbot.addWidget(bar)
    width = FilterField("width", "Width", "range", int)
    folder = FilterField("folder", "Folder", "text", str)
    bar.set_filter_fields([width, folder])
    bar.set_field_filter("width", RangeFilter("width", 500, None))
    assert bar.filters().fields == (RangeFilter("width", 500, None),)
    bar.set_filter_fields([folder])  # say the scope now includes fonts, which have no width
    chip = bar._field_chips["width"]
    assert not chip.available
    assert "Not applied" in chip.toolTip()
    assert bar.filters().fields == ()
    bar.set_filter_fields([width, folder])
    assert bar.filters().fields == (RangeFilter("width", 500, None),)
