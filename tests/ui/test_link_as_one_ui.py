"""Link as one from the Dedupe page, and Unlink on an item's page (#600), on the music
demo."""

import pytest
from pytestqt.qtbot import QtBot

from tagalot.core.dedupe import Verification
from tagalot.core.detail import FileRow
from tagalot.core.session import KeepSession
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tests.ui.test_merge_ui import _two_blues
from tests.ui.test_music_views import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


def _places(page: DetailPage) -> list[FileRow]:
    assert page.detail is not None
    return [f for s in page.detail.sections for f in s.files if f.place_of is not None]


def test_link_as_one_then_unlink(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _two_blues(qtbot, window, session)
    assert page.link_button.isVisible()
    assert page.link_button.isEnabled()
    pane = page.compare_pane
    assert pane.comparison is not None
    first, _ = (i.id for i in pane.comparison.items)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        page.link_button.click()
    assert blocker.args == ["Link 2 copies as one. Edit → Undo separates them again."]
    # The page refreshed: places of one file aren't duplicates.
    qtbot.waitUntil(lambda: page.groups == [], timeout=5000)

    window.open_entity(first)
    detail = window.stack.currentWidget()
    assert isinstance(detail, DetailPage)
    qtbot.waitUntil(lambda: detail.detail is not None and bool(_places(detail)), timeout=5000)
    [place] = _places(detail)
    assert place.relpath == "Jazz Hits/02 Blue.mp3"  # the group lists Copies first
    menu = detail._file_menu(place)
    [split] = [a for a in menu.actions() if a.text() == "Unlink this place"]
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        split.trigger()
    assert blocker.args == ["Unlink 02 Blue.mp3: it has an item of its own now."]
    qtbot.waitUntil(lambda: detail.detail is not None and not _places(detail), timeout=5000)


def test_not_once_check_found_the_copies_different(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _two_blues(qtbot, window, session)
    group = page._current_group()
    assert group is not None
    ids = tuple(f.resource_id for f in group.files)
    page.checks[group.fingerprint] = Verification(identical=(), different=ids)
    page._show_link()
    assert page.link_button.isVisible()
    assert not page.link_button.isEnabled()
    assert "different" in page.link_button.toolTip()
