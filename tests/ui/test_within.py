"""Drilling down: "Show contents in search" and the Within chip (#89)."""

import pytest
from PySide6.QtWidgets import QPushButton
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import Entity
from tagalot.core.search import SearchHit
from tagalot.core.search_fields import contained_types
from tagalot.core.session import KeepSession
from tagalot.ui.filter_bar import FilterBar
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tests.ui.test_contents_search import _open, session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


def _search(qtbot: QtBot, window: MainWindow, target: NavTarget, count: str) -> SearchPage:
    window.navigation.select(target)
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    qtbot.waitUntil(lambda: page.status.text() == count, timeout=5000)
    return page


def _hit(page: SearchPage, title: str) -> SearchHit:
    if page.showing_groups():
        for section in page.groups.sections:
            for row in range(section.model.rowCount()):
                hit = section.model.hit(row)
                if hit is not None and hit.title == title:
                    return hit
    for row in range(page.model.rowCount()):
        hit = page.model.hit(row)
        if hit is not None and hit.title == title:
            return hit
    raise AssertionError(title)


def _drill(page: SearchPage, title: str) -> None:
    menu = page.item_menu(_hit(page, title))
    action = next(a for a in menu.actions() if a.text() == "Show contents in search")
    assert action.isEnabled()
    action.trigger()


SEARCH_ALL = NavTarget("search", label="Search all")


def test_contained_types(session: KeepSession) -> None:
    assert contained_types(session.schema, "assets2d.artist") == [
        "assets2d.image",
        "assets2d.font",
        "assets2d.archive",
    ]
    assert contained_types(session.schema, "assets2d.image") == []


def test_drilling_down_in_search_all_keeps_the_tag_filters(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _search(qtbot, window, SEARCH_ALL, "14 items")
    _drill(page, "Aurora Studio")
    qtbot.waitUntil(lambda: page.status.text() == "7 items", timeout=5000)
    assert page.showing_groups()  # still grouped: Images, Fonts, Archives
    assert [s.group.label for s in page.groups.sections] == ["Images", "Fonts", "Archives"]
    assert page.filter_bar.filters().within is not None

    tree = session.tag_cache.get()
    use = tree.find_child(None, "Use")
    background = tree.find_child(use, "Background")
    assert background is not None
    page.filter_bar.add_tag(background)
    qtbot.waitUntil(lambda: page.status.text() == "2 items", timeout=5000)

    page.filter_bar.set_within(None)  # the chip's close button
    qtbot.waitUntil(lambda: page.status.text() == "2 items", timeout=5000)  # tag kept
    page.filter_bar.clear()
    qtbot.waitUntil(lambda: page.status.text() == "14 items", timeout=5000)


def test_a_views_types_are_kept_when_the_container_holds_them(
    qtbot: QtBot, window: MainWindow
) -> None:
    images = _search(qtbot, window, NavTarget("view", key="Images", label="Images"), "9 items")
    images.show_within(_hit_id(window, "Aurora Studio"), "Aurora Studio", "assets2d.artist")
    qtbot.waitUntil(lambda: images.status.text() == "5 items", timeout=5000)


def test_otherwise_everything_the_container_holds(qtbot: QtBot, window: MainWindow) -> None:
    artists = _search(qtbot, window, NavTarget("view", key="Artists", label="Artists"), "2 items")
    _drill(artists, "Kenji Sato")
    qtbot.waitUntil(lambda: artists.status.text() == "4 items", timeout=5000)
    assert "type" in [c.key for c in artists.model.columns]  # images and an archive


def test_items_that_contain_nothing(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, SEARCH_ALL, "14 items")
    menu = page.item_menu(_hit(page, "forest.png"))
    open_action, within = menu.actions()[:2]
    assert open_action.text() == "Open"
    assert not within.isEnabled()
    with qtbot.waitSignal(page.open_requested) as blocker:
        open_action.trigger()
    assert blocker.args == [_hit(page, "forest.png").id]


def test_the_grid_menu_starts_with_the_cards_actions(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, NavTarget("view", key="Images", label="Images"), "9 items")
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    hit = page.model.hit(0)
    labels = [a.text() for a in page.grid_menu(hit).actions()]
    assert labels[:3] == ["Open", "Show contents in search", ""]
    assert "Card lines" in labels
    assert "Open" not in [a.text() for a in page.grid_menu().actions()]  # empty space


def test_show_in_search_from_a_detail_page(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    detail = _open(qtbot, window, session, "Kenji Sato")
    assert detail.contents is not None
    button = next(
        b for b in detail.contents.findChildren(QPushButton) if b.text() == "Show in search"
    )
    button.click()
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    assert page.grouped
    qtbot.waitUntil(lambda: page.status.text() == "4 items", timeout=5000)
    assert page.filter_bar.filters().within == detail.entity_id


def test_the_chip_comes_first_and_clear_all_removes_it(qtbot: QtBot) -> None:
    bar = FilterBar()
    qtbot.addWidget(bar)
    bar.set_only("assets2d.image", "Images")
    bar.set_within(7, "Aurora Studio", "assets2d.artist")
    labels = [
        bar.chip_layout.itemAt(i).widget().label.text()  # type: ignore[union-attr]
        for i in range(2)
    ]
    assert labels == ["Within: Aurora Studio", "Only: Images"]
    filters = bar.filters()
    assert (filters.within, filters.within_type, filters.only) == (
        7,
        "assets2d.artist",
        "assets2d.image",
    )
    with qtbot.waitSignal(bar.changed):
        bar.clear()
    assert (bar.filters().within, bar.filters().only) == (None, None)


def _hit_id(window: MainWindow, title: str) -> int:
    session = window.session
    assert session is not None
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None
    return found
