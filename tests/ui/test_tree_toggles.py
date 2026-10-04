"""The tree layout and the "Contained" / "Inherit tags" toggles (#91)."""

import pytest
from PySide6.QtCore import QItemSelectionModel, QModelIndex, QPointF, Qt, QThreadPool
from PySide6.QtGui import QDropEvent
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.ui_state import load_ui_state
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.dnd import tags_mime
from tagalot.ui.main_window import MainWindow
from tagalot.ui.models.results import TAGS
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import ScanController
from tests.ui.test_contents_search import _id, session, window
from tests.ui.test_within import _search

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

ARTISTS = NavTarget("view", key="Artists", label="Artists")
IMAGES = NavTarget("view", key="Images", label="Images")


def _titles(page: SearchPage) -> list[str]:
    hits = [page.model.hit(r) for r in range(page.model.rowCount())]
    return sorted(h.title for h in hits if h is not None)


def _tree_titles(page: SearchPage, parent: QModelIndex | None = None) -> list[str]:
    model = page.tree_model
    parent = parent or QModelIndex()
    return [str(model.index(r, 0, parent).data()) for r in range(model.rowCount(parent))]


def _tree_row(page: SearchPage, title: str, parent: QModelIndex | None = None) -> QModelIndex:
    model = page.tree_model
    parent = parent or QModelIndex()
    for row in range(model.rowCount(parent)):
        index = model.index(row, 0, parent)
        if index.data() == title:
            return index
    raise AssertionError(title)


def _tree(qtbot: QtBot, window: MainWindow) -> tuple[SearchPage, QModelIndex]:
    """The Artists view as a tree, with Aurora Studio expanded."""
    page = _search(qtbot, window, ARTISTS, "2 items")
    page.tree_button.click()
    assert page.results.currentWidget() is page.tree
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    aurora = _tree_row(page, "Aurora Studio")
    page.tree.expand(aurora)
    qtbot.waitUntil(lambda: page.tree_model.rowCount(aurora) == 7, timeout=5000)
    return page, aurora


# --- toggles ---


def test_toggles_start_with_the_views_defaults(qtbot: QtBot, window: MainWindow) -> None:
    images = _search(qtbot, window, IMAGES, "9 items")
    assert images.filter_bar.inherit_box.isChecked()  # assets2d's asset views inherit
    assert not images.filter_bar.contained_box.isChecked()
    artists = _search(qtbot, window, ARTISTS, "2 items")
    assert not artists.filter_bar.inherit_box.isChecked()


def test_contained_lists_what_the_matches_hold_and_is_remembered(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    artists = _search(qtbot, window, ARTISTS, "2 items")
    artists.filter_bar.contained_box.setChecked(True)
    qtbot.waitUntil(lambda: artists.status.text() == "13 items", timeout=5000)  # 2 + 7 + 4
    assert "forest.png" in _titles(artists)
    state = load_ui_state(session.keep.ui_state_path)
    assert state["toggles"] == {
        "view:Artists": {"show_contained": True, "inherit_tags": False, "aggregate_up": False}
    }

    again = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(again)
    again.navigation.select(ARTISTS)
    page = again.stack.currentWidget()
    assert isinstance(page, SearchPage)
    assert page.filter_bar.contained_box.isChecked()
    qtbot.waitUntil(lambda: page.status.text() == "13 items", timeout=5000)
    again.thumbnails.clear()
    again.thumbnails.wait()


def test_inherit_tags_counts_an_artists_tags(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    tree = session.tag_cache.get()
    favorites = tree.find_child(None, "Favorites")
    assert favorites is not None
    kenji = _id(session, "Kenji Sato")
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.apply_tags([kenji], [favorites])

    images = _search(qtbot, window, IMAGES, "9 items")
    images.filter_bar.add_tag(favorites)
    # forest.png is tagged itself; Kenji's three images inherit the artist's tag.
    qtbot.waitUntil(lambda: images.status.text() == "4 items", timeout=5000)
    images.filter_bar.inherit_box.setChecked(False)
    qtbot.waitUntil(lambda: images.status.text() == "1 item", timeout=5000)
    assert _titles(images) == ["forest.png"]


def test_by_contents_finds_artists_by_their_images(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    """ "By contents" (#133): an artist matches when one of their images does."""
    tree = session.tag_cache.get()
    favorites = tree.find_child(None, "Favorites")
    assert favorites is not None
    artists = _search(qtbot, window, ARTISTS, "2 items")
    assert not artists.filter_bar.aggregate_box.isChecked()  # off unless the view says
    artists.filter_bar.add_tag(favorites)
    qtbot.waitUntil(lambda: artists.status.text() == "Nothing found", timeout=5000)
    artists.filter_bar.aggregate_box.setChecked(True)
    # forest.png is tagged Favorites, and it is Aurora Studio's.
    qtbot.waitUntil(lambda: artists.status.text() == "1 item", timeout=5000)
    assert _titles(artists) == ["Aurora Studio"]
    state = load_ui_state(session.keep.ui_state_path)
    assert state["toggles"]["view:Artists"]["aggregate_up"] is True  # remembered

    saved = artists.saved_definition()
    assert saved.filters.aggregate_up
    artists.filter_bar.aggregate_box.setChecked(False)
    artists.restore(saved)
    assert artists.filter_bar.aggregate_box.isChecked()  # a saved search brings it back


def test_clear_all_keeps_the_toggles(qtbot: QtBot, window: MainWindow) -> None:
    images = _search(qtbot, window, IMAGES, "9 items")
    images.filter_bar.contained_box.setChecked(True)
    images.filter_bar.clear()
    assert images.filter_bar.contained_box.isChecked()
    assert images.filter_bar.inherit_box.isChecked()


# --- the tree ---


def test_the_tree_expands_containers(qtbot: QtBot, window: MainWindow) -> None:
    page, aurora = _tree(qtbot, window)
    assert _tree_titles(page) == ["Aurora Studio", "Kenji Sato"]
    assert _tree_titles(page, aurora) == [  # by title, ignoring case
        "Aileron-Regular.ttf",
        "dusk sky.png",
        "folder.jpg",
        "forest.png",
        "gem.png",
        "heart.png",
        "UI pack.zip",
    ]
    forest = _tree_row(page, "forest.png", aurora)
    assert not page.tree_model.hasChildren(forest)  # an image holds nothing
    columns = [c.key for c in page.model.columns]
    assert columns[:2] == ["title", "type"]  # children's types show in the tree
    assert page.tree_model.index(forest.row(), 1, aurora).data() == "Image"
    assert page.tree_model.hasChildren(_tree_row(page, "Kenji Sato"))
    assert load_ui_state(window.session.keep.ui_state_path)["layouts"] == {  # type: ignore[union-attr]
        "view:Artists": "tree"
    }


def test_contained_is_off_in_the_tree(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, ARTISTS, "2 items")
    page.filter_bar.contained_box.setChecked(True)
    qtbot.waitUntil(lambda: page.status.text() == "13 items", timeout=5000)
    page.tree_button.click()
    assert not page.filter_bar.contained_box.isEnabled()
    qtbot.waitUntil(lambda: page.status.text() == "2 items", timeout=5000)  # not flattened
    page.list_button.click()
    assert page.filter_bar.contained_box.isEnabled()
    qtbot.waitUntil(lambda: page.status.text() == "13 items", timeout=5000)
    assert "type" in [c.key for c in page.model.columns]  # contents of other types listed
    page.filter_bar.contained_box.setChecked(False)
    qtbot.waitUntil(lambda: page.status.text() == "2 items", timeout=5000)
    assert "type" not in [c.key for c in page.model.columns]  # only artists: no Type column


def test_selecting_and_opening_in_the_tree(qtbot: QtBot, window: MainWindow) -> None:
    page, aurora = _tree(qtbot, window)
    forest = _tree_row(page, "forest.png", aurora)
    page.tree.selectionModel().select(
        forest,
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    ids: list[list[int]] = []
    page.selected_entity_ids(ids.append)
    forest_id = page.tree_model.hit(forest).id  # type: ignore[union-attr]
    assert ids == [[forest_id]]
    qtbot.waitUntil(lambda: page.preview.title.text().startswith("forest.png"), timeout=5000)

    labels = [a.text() for a in page.item_menu(page.tree_model.hit(forest)).actions()]  # type: ignore[arg-type]
    assert labels[:2] == ["Open page", "Show contents in search"]
    page._tree_activated(forest, alternate=True)  # an image's page: Ctrl+Enter
    detail = window.stack.currentWidget()
    assert isinstance(detail, DetailPage)
    assert detail.entity_id == forest_id


def test_tags_dropped_on_a_child(qtbot: QtBot, window: MainWindow) -> None:
    page, aurora = _tree(qtbot, window)
    gem = _tree_row(page, "gem.png", aurora)
    page.tree.scrollTo(gem)
    center = page.tree.visualRect(gem).center()
    mime = tags_mime([1])  # the event only points to it: keep it alive
    event = QDropEvent(
        QPointF(center),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    with qtbot.waitSignal(page.tags_dropped) as blocker:
        page.tree.dropEvent(event)
    assert blocker.args == [[page.tree_model.hit(gem).id], [1]]  # type: ignore[union-attr]


def test_search_all_has_no_tree(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, NavTarget("search", label="Search all"), "14 items")
    assert page.showing_groups()
    assert not page.tree_button.isEnabled()


# --- the Tags column after tagging ---


def _show_tags_column(page: SearchPage) -> int:
    page._column_toggled(TAGS, True)
    column = [c.key for c in page.model.columns].index(TAGS)
    return column


def test_tags_column_updates_in_the_list_after_tagging(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _search(qtbot, window, IMAGES, "9 items")
    column = _show_tags_column(page)
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    row = next(r for r in range(9) if page.model.hit(r).title == "gem.png")  # type: ignore[union-attr]
    favorites = session.tag_cache.get().find_child(None, "Favorites")
    assert favorites is not None
    assert "Favorites" not in str(page.model.index(row, column).data())
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.apply_tags([_id(session, "gem.png")], [favorites])
    qtbot.waitUntil(lambda: "Favorites" in str(page.model.index(row, column).data()), timeout=5000)


def test_tags_column_updates_in_the_tree_after_tagging(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page, aurora = _tree(qtbot, window)
    column = _show_tags_column(page)
    gem = _tree_row(page, "gem.png", aurora)
    favorites = session.tag_cache.get().find_child(None, "Favorites")
    assert favorites is not None
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.apply_tags([_id(session, "gem.png")], [favorites])

    def updated() -> None:
        cell = page.tree_model.index(gem.row(), column, aurora).data()
        assert "Favorites" in str(cell)

    qtbot.waitUntil(updated, timeout=5000)


def test_a_child_that_becomes_excluded_leaves_the_tree(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page, aurora = _tree(qtbot, window)
    favorites = session.tag_cache.get().find_child(None, "Favorites")
    assert favorites is not None
    page.filter_bar.add_tag(favorites, exclude=True)  # excluded children are left out
    qtbot.waitUntil(lambda: page.status.text() == "2 items", timeout=5000)
    aurora = _tree_row(page, "Aurora Studio")
    page.tree.expand(aurora)
    qtbot.waitUntil(lambda: page.tree_model.rowCount(aurora) == 5, timeout=5000)  # no forest, font
    assert "gem.png" in _tree_titles(page, aurora)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.apply_tags([_id(session, "gem.png")], [favorites])
    qtbot.waitUntil(lambda: page.tree_model.rowCount(aurora) == 4, timeout=5000)
    assert "gem.png" not in _tree_titles(page, aurora)
