"""Re-reading items from their files, from menus (#96)."""

import pytest
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tests.ui.test_contents_search import _id, _open, session, window
from tests.ui.test_field_editing import FONT, _field
from tests.ui.test_within import _hit, _search

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

REREAD = "Re-read from file"
REPLACE = "Re-read from file, replacing my edits\u2026"


def _edit_family(qtbot: QtBot, window: MainWindow, session: KeepSession) -> DetailPage:
    page = _open(qtbot, window, session, FONT)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.tag_actions.edit_field(_id(session, FONT), "family", "Aileron Sans")
    qtbot.waitUntil(lambda: _field(page, "family").label.text() == "Aileron Sans", timeout=5000)
    return page


def _action(page: DetailPage, text: str) -> object:
    menu = page.more_button.menu()
    assert menu is not None
    return next(a for a in menu.actions() if a.text() == text)


def test_rereading_keeps_edits(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _edit_family(qtbot, window, session)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=10_000) as blocker:
        _action(page, REREAD).trigger()  # type: ignore[attr-defined]
    assert blocker.args == ["Re-read 1 item from their files."]
    assert _field(page, "family").label.text() == "Aileron Sans"


def test_replacing_edits_asks_first_and_can_be_undone(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _edit_family(qtbot, window, session)
    asked: list[int] = []

    def no(count: int) -> bool:
        asked.append(count)
        return False

    monkeypatch.setattr(window, "confirm_replace_edits", no)
    _action(page, REPLACE).trigger()  # type: ignore[attr-defined]
    assert asked == [1]
    assert _field(page, "family").label.text() == "Aileron Sans"  # cancelled

    monkeypatch.setattr(window, "confirm_replace_edits", lambda count: True)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=10_000) as blocker:
        _action(page, REPLACE).trigger()  # type: ignore[attr-defined]
    assert blocker.args == ["Re-read 1 item from their files, replacing your edits."]
    qtbot.waitUntil(lambda: _field(page, "family").label.text() == "Aileron", timeout=5000)
    assert _field(page, "family").marker.isHidden()

    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.undo_action.trigger()
    qtbot.waitUntil(lambda: _field(page, "family").label.text() == "Aileron Sans", timeout=5000)
    assert not _field(page, "family").marker.isHidden()


def test_the_results_menu_rereads_the_selection(qtbot: QtBot, window: MainWindow) -> None:
    page = _search(qtbot, window, NavTarget("view", key="Images", label="Images"), "9 items")
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    labels = [a.text() for a in page.item_menu(_hit(page, "gem.png")).actions()]
    assert labels[-2:] == [REREAD, REPLACE]
    page.table.selectAll()
    with qtbot.waitSignal(page.reread_requested) as blocker:
        next(
            a for a in page.item_menu(_hit(page, "gem.png")).actions() if a.text() == REREAD
        ).trigger()
    ids, replace = blocker.args
    assert (len(ids), replace) == (9, False)  # the whole selection
