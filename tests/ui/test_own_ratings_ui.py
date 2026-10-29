"""Ratings for any type in the window (#577): turned on from the type's menu, five clickable
stars on the page, and turned off after asking, as an undo step."""

import pytest
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tests.ui.test_field_columns import _books, session
from tests.ui.test_field_editing import _field

pytestmark = pytest.mark.gui

__all__ = ["session"]  # the fixture

COLLECTION = "books.collection"
TRILOGY = "The Mistborn Trilogy"


def _window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = _books(qtbot, session).window()
    assert isinstance(window, MainWindow)
    return window


def test_rating_a_type_from_the_window(
    qtbot: QtBot, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window(qtbot, session)
    window.set_rating(COLLECTION, True)
    qtbot.waitUntil(lambda: session.own_rating(COLLECTION) is not None, timeout=5000)
    qtbot.waitUntil(lambda: "can be rated now" in window.statusBar().currentMessage())

    with session.reader.connect() as conn:
        trilogy = conn.scalar(select(Entity.id).where(Entity.title == TRILOGY))
    assert trilogy is not None
    window.open_entity(trilogy)
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: page.detail is not None, timeout=5000)
    rating = _field(page, "own:rating")
    assert rating.stars is not None  # five clickable stars, as a theme's rating
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        rating.stars.chosen.emit(7)  # three and a half stars

    def shown() -> bool:
        stars = _field(page, "own:rating").stars
        return stars is not None and stars.halves == 7

    qtbot.waitUntil(shown, timeout=5000)

    asked: list[str] = []

    def refuse(plural: str) -> bool:
        asked.append(plural)
        return False

    monkeypatch.setattr(window, "confirm_unrate", refuse)
    window.set_rating(COLLECTION, False)  # not confirmed: nothing changes
    assert asked == ["Collections"]
    assert session.own_rating(COLLECTION) is not None
    monkeypatch.setattr(window, "confirm_unrate", lambda plural: True)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        window.set_rating(COLLECTION, False)
    assert blocker.args[0].startswith("Turn 'Rating' back into an Extra field (1 item)")
    assert session.own_rating(COLLECTION) is None


def test_a_type_the_theme_rates_isnt_offered(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    assert session.theme_rates("books.book")
    window.set_rating("books.book", True)
    qtbot.waitUntil(lambda: "Couldn't turn on ratings" in window.statusBar().currentMessage())
    assert session.own_rating("books.book") is None
