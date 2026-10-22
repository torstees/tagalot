"""Tag icons in the Tag manager, and badges on items (#398).

The demo keep's Favorites has a star, Places a globe (Iceland and Beach show it), and
Topics > Money a money bag.
"""

import importlib.util
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import PATH_SEPARATOR
from tagalot.ui.badges import GAP, badges_that_fit
from tagalot.ui.main_window import MainWindow
from tagalot.ui.models.results import BADGES, BadgesValue, row_values
from tagalot.ui.navigation import NavTarget
from tagalot.ui.result_table import list_columns
from tagalot.ui.search_view import SearchPage
from tagalot.ui.tag_manager import TagManagerPage
from tests.ui.test_detail_page import _open
from tests.ui.test_grid import (
    SCRIPT,
    _close,
    _grid_page,
    _page,
    _row,
    _window,
    keep_dir,
    session,
)

pytestmark = pytest.mark.gui

__all__ = ["keep_dir", "session"]  # fixtures

STAR, GLOBE, MONEY, FAMILY_ICON = "⭐", "\U0001f30d", "\U0001f4b0", "\U0001f46a"


def _more(n: int) -> int:
    return 10 + 6 * len(str(n))  # "+n" grows with its digits


SIZES = {
    "all fit": (3, 60, 3),
    "exactly": (3, 3 * 18 + 2 * GAP, 3),
    "one short": (5, 60, 2),  # 2 badges (40) and "+3" (16)
    "only +n": (4, 15, 0),
    "nothing": (0, 100, 0),
    "a large thumbnail": (14, 256, 12),  # 12 badges (240) and "+2" (16)
}


@pytest.mark.parametrize("case", SIZES.values(), ids=SIZES.keys())
def test_as_many_badges_as_fit_then_more(case: tuple[int, int, int]) -> None:
    count, width, shown = case
    assert badges_that_fit(count, width, 18, _more) == shown


def _cell(page: SearchPage, title: str, role: int = Qt.ItemDataRole.DisplayRole) -> str:
    column = [c.key for c in page.model.columns].index(BADGES)
    value: str = page.model.index(_row(page, title), column).data(role)
    return value


def test_the_badges_column(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    page = _page(window)
    qtbot.waitUntil(lambda: _cell(page, "Family at the beach.jpg") != "", timeout=5000)
    # Beach shows Places' globe; Family (People) has no icon; Favorites its star.
    assert _cell(page, "Family at the beach.jpg") == f"{STAR} {GLOBE}"
    tooltip = _cell(page, "Family at the beach.jpg", Qt.ItemDataRole.ToolTipRole)
    assert tooltip == f"{STAR} Favorites\n{GLOBE} Places{PATH_SEPARATOR}Beach"
    assert _cell(page, "sunset.jpg") == GLOBE  # Beach and Sky: one globe, Sky has none
    assert _cell(page, "tax 2025.pdf") == MONEY
    assert _cell(page, "notes.txt") == ""
    column = [c.key for c in page.model.columns].index(BADGES)
    assert not page.table.isColumnHidden(column)  # shown, beside the title
    _close(window)


def test_grid_cards_paint_badges(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    page = _grid_page(qtbot, window)
    found = page.model.row(_row(page, "glacier.jpg"))
    assert found is not None
    assert found[1][BADGES] == BadgesValue(
        f"{STAR} {GLOBE}",
        f"{STAR} Favorites\n{GLOBE} Places{PATH_SEPARATOR}Iceland",
        (STAR, GLOBE),
    )
    assert not page.grid.grab().isNull()  # painted, badges and all
    _close(window)


def test_the_item_page_shows_badges_beside_the_title(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    detail = _open(qtbot, window, "Family at the beach.jpg")
    qtbot.waitUntil(lambda: detail.badges.text() != "", timeout=5000)
    assert detail.badges.text() == f"{STAR} {GLOBE}"
    assert detail.badges.toolTip().startswith(f"{STAR} Favorites")
    _close(window)


def test_container_tags_give_no_badges(tmp_path: Path) -> None:
    """Only an item's own tags give badges, even with Inherit tags on: an album's star
    would crowd every song's own."""
    script = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert script is not None
    assert script.loader is not None
    module = importlib.util.module_from_spec(script)
    script.loader.exec_module(module)
    with KeepSession.open(module.make_music_demo(tmp_path), Settings()) as music:
        star = music.tags.add(None, "Best albums")
        music.tags.set_icon(star, STAR)
        tree = music.tag_cache.get()
        with music.reader.connect() as conn:
            album = run_search(conn, SearchSpec(types=("music.album",)), tree)[0]
        music.tags.apply([album.id], [star])
        tree = music.tag_cache.get()
        with music.reader.connect() as conn:
            spec = SearchSpec(types=("music.song",), within=album.id, inherit_tags=True)
            songs = run_search(conn, spec, tree)
            columns = list_columns(music.schema, ("music.song",))
            values = row_values(conn, music.schema, tree, songs, columns)
            album_values = row_values(conn, music.schema, tree, [album], columns)
        assert songs
        assert all(values[s.id][BADGES] == BadgesValue("", "") for s in songs)
        assert album_values[album.id][BADGES].text == STAR


def _manager(qtbot: QtBot, window: MainWindow) -> TagManagerPage:
    window.navigation.select(NavTarget("tags", label="Tag manager"))
    page = window.stack.currentWidget()
    assert isinstance(page, TagManagerPage)
    qtbot.waitUntil(lambda: page.loaded, timeout=5000)
    return page


def _select(page: TagManagerPage, session: KeepSession, name: str) -> None:
    tree = session.tag_cache.get()
    [tag_id] = [t for t in tree if tree.node(t).name == name]
    assert page.select_tag(tag_id)


def _manager_icon(page: TagManagerPage, name: str) -> str | None:
    """The icon of the tag called ``name`` in the tree the Tag manager shows."""
    tree = page.model.tree
    if tree is None:
        return None
    [tag_id] = [t for t in tree if tree.node(t).name == name]
    return tree.node(tag_id).icon


def _wait_status(qtbot: QtBot, window: MainWindow, expected: str) -> None:
    def check() -> None:
        assert window.statusBar().currentMessage() == expected

    qtbot.waitUntil(check, timeout=5000)


def test_setting_an_icon_in_the_tag_manager(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    page = _manager(qtbot, window)
    details = page.details

    _select(page, session, "Favorites")
    assert details.icon_edit.text() == STAR
    _select(page, session, "Iceland")
    assert details.icon_edit.text() == ""
    assert details.icon_note.text() == f"{GLOBE} from 'Places'"  # inherited

    _select(page, session, "People")
    assert details.icon_note.text() == "None"
    details.icon_edit.setText(FAMILY_ICON)
    details.icon_edit.editingFinished.emit()  # as Enter, or leaving the box, does
    _wait_status(qtbot, window, f"Set the icon of 'People' to {FAMILY_ICON}.")
    qtbot.waitUntil(lambda: f"{FAMILY_ICON} People" in _panel_names(window), timeout=5000)
    # The manager reloads its own tree in a worker, apart from the panel: wait for it too,
    # or Family's note is worked out from the tree before the change (seen on Windows CI).
    qtbot.waitUntil(lambda: _manager_icon(page, "People") == FAMILY_ICON, timeout=5000)
    _select(page, session, "Family")
    assert details.icon_note.text() == f"{FAMILY_ICON} from 'People'"

    _select(page, session, "People")
    details.icon_edit.setText("ab")
    details.icon_edit.editingFinished.emit()
    _wait_status(qtbot, window, "An icon is one emoji or one character.")
    qtbot.waitUntil(lambda: details.icon_edit.text() == FAMILY_ICON, timeout=5000)

    assert window.tag_actions.undo_label == "Change the icon of 'People'"
    window.tag_actions.undo()
    qtbot.waitUntil(lambda: "People" in _panel_names(window), timeout=5000)

    def reloaded() -> bool:  # the manager reloads its tree in the background
        tree = page.model.tree
        return tree is not None and not any(
            tree.node(t).name == "People" and tree.node(t).icon for t in tree
        )

    qtbot.waitUntil(reloaded, timeout=5000)
    _select(page, session, "People")
    assert details.icon_edit.text() == ""

    _select(page, session, "Favorites")
    details.clear_icon.click()
    _wait_status(qtbot, window, "Cleared the icon of 'Favorites'.")
    _close(window)


def _panel_names(window: MainWindow) -> list[str]:
    model = window.tag_panel.model
    return [str(model.index(r, 0).data()) for r in range(model.rowCount())]


def test_the_badges_column_follows_whether_any_tag_has_an_icon(
    qtbot: QtBot, session: KeepSession
) -> None:
    window = _window(qtbot, session)
    page = _page(window)
    column = [c.key for c in page.model.columns].index(BADGES)
    assert not page.table.isColumnHidden(column)
    tree = session.tag_cache.get()
    with_icons = [t for t in tree if tree.node(t).icon]
    for tag_id in with_icons:
        session.tags.set_icon(tag_id, None)
    page.refresh()  # as the window does after a tag change
    qtbot.waitUntil(lambda: page.table.isColumnHidden(column), timeout=5000)  # no empty column
    session.tags.set_icon(with_icons[0], STAR)
    page.refresh()
    qtbot.waitUntil(lambda: not page.table.isColumnHidden(column), timeout=5000)
    _close(window)
