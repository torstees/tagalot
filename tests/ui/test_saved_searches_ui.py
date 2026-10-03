"""Saved searches in the window (#127): save a view with its chips, open it, update it,
rename and delete it, and undo, on the movies demo."""

import pytest
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tests.ui.test_movie_pages import session, window
from tests.ui.test_within import _search

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


def _tag(session: KeepSession, name: str) -> int:
    tree = session.tag_cache.get()
    return next(t for t in tree if tree.node(t).name == name)


def _saved(window: MainWindow) -> dict[str, int]:
    return {name: saved_id for saved_id, (name, _) in window._saved.items()}


def test_saving_opening_and_managing_a_search(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    movies = _search(qtbot, window, NavTarget("view", key="Movies", label="Movies"), "9 items")
    movies.filter_bar.add_tag(_tag(session, "Sci-Fi"))
    movies.filter_bar.add_tag(_tag(session, "Watched"), exclude=True)  # Alien
    qtbot.waitUntil(lambda: movies.status.text() == "2 items", timeout=5000)
    asked: list[tuple[str, str]] = []

    def name(title: str, default: str) -> str | None:
        asked.append((title, default))
        return "Unwatched sci-fi"

    window.choose_saved_name = name  # type: ignore[method-assign]
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        movies.save_button.click()
    assert asked == [("Save search", "Movies")]  # the page's title, to start from
    assert blocker.args == ["Save the search 'Unwatched sci-fi'."]
    qtbot.waitUntil(lambda: "Unwatched sci-fi" in _saved(window), timeout=5000)
    saved_id = _saved(window)["Unwatched sci-fi"]
    target = NavTarget("saved", key=str(saved_id), label="Unwatched sci-fi")
    assert target in window.navigation.targets("saved")

    # Opening it: the view's search, the chips back (each can still be removed), the grid.
    window.navigation.select(target)
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    assert page is not movies
    qtbot.waitUntil(lambda: page.status.text() == "2 items", timeout=5000)
    assert page.layout_mode == "grid"
    filters = page.filter_bar.filters()
    assert filters.include == (_tag(session, "Sci-Fi"),)
    assert filters.exclude == (_tag(session, "Watched"),)
    page.filter_bar.remove_tag(_tag(session, "Watched"))
    qtbot.waitUntil(lambda: page.status.text() == "3 items", timeout=5000)

    # Ctrl+S on a saved search's page updates it, asking nothing.
    asked.clear()
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        window.save_search_action.trigger()
    assert asked == []
    assert blocker.args == ["Update the saved search 'Unwatched sci-fi'."]

    # Saving another search under a taken name asks first; No leaves both alone.
    replaced: list[str] = []

    def replace(name: str) -> bool:
        replaced.append(name)
        return False

    window.confirm_replace_saved = replace  # type: ignore[method-assign]
    window.save_search(as_new=False, page=movies)
    assert replaced == ["Unwatched sci-fi"]

    # Rename, then delete: its page closes; undo brings it back.
    window.choose_saved_name = lambda title, default: "Sci-fi"  # type: ignore[method-assign]
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.rename_saved(saved_id)
    qtbot.waitUntil(lambda: "Sci-fi" in _saved(window), timeout=5000)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        window.tag_actions.delete_saved(saved_id)
    assert blocker.args == ["Delete the saved search 'Sci-fi'. Edit → Undo brings it back."]
    qtbot.waitUntil(lambda: _saved(window) == {}, timeout=5000)
    qtbot.waitUntil(lambda: page not in window._pages.values(), timeout=5000)
    assert window.navigation.targets("saved") == []
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.tag_actions.undo()
    qtbot.waitUntil(lambda: _saved(window) == {"Sci-fi": saved_id}, timeout=5000)


def test_a_search_all_saves_with_its_sections(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    window.navigation.select(NavTarget("search", label="Search all"))
    search = window.stack.currentWidget()
    assert isinstance(search, SearchPage)
    # (no chips: several types match, so it shows sections; one type shows a list)
    window.choose_saved_name = lambda title, default: "Everything"  # type: ignore[method-assign]
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.save_search_action.trigger()
    qtbot.waitUntil(lambda: "Everything" in _saved(window), timeout=5000)
    window.navigation.select(
        NavTarget("saved", key=str(_saved(window)["Everything"]), label="Everything")
    )
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    assert page.grouped
    qtbot.waitUntil(page.showing_groups, timeout=5000)
