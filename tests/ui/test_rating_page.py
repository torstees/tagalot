"""A book's rating on its page (#419), on the books demo: The Colour of Magic's EPUB rates
it four stars (Calibre's 8 out of 10); clicking a star changes it, as the user's."""

import pytest
from pytestqt.qtbot import QtBot

from tagalot.ui.field_editor import EditableValue
from tagalot.ui.main_window import MainWindow
from tests.ui.test_contents_search import _open
from tests.ui.test_file_extra_page import window

pytestmark = pytest.mark.gui

__all__ = ["window"]  # fixtures


def _rating(page: object) -> float | None:
    detail = getattr(page, "detail", None)
    if detail is None:
        return None
    return next((f.value for s in detail.sections for f in s.fields if f.name == "rating"), None)


def test_rating_a_book_on_its_page(qtbot: QtBot, window: MainWindow) -> None:
    session = window.session
    assert session is not None
    page = _open(qtbot, window, session, "The Colour of Magic")
    rating = page.details.findChild(EditableValue, "field_rating")
    assert isinstance(rating, EditableValue)
    assert rating.stars is not None
    assert rating.stars.halves == 8  # four stars, from the file
    assert rating.editable

    rating.stars.chosen.emit(9)  # as clicking the fifth star's left half does
    qtbot.waitUntil(lambda: _rating(page) == 4.5, timeout=5000)
    assert window.statusBar().currentMessage().startswith("Set Rating")
    window.tag_actions.undo()
    qtbot.waitUntil(lambda: _rating(page) == 4.0, timeout=5000)
