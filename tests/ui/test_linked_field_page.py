"""Editing a field that mirrors a link on an item's page (#440), on the books demo: a
book's Series edits like any field and moves the book; a universe it gets through its
series is shown, not edited, and says why."""

import pytest
from pytestqt.qtbot import QtBot

from tagalot.ui.field_editor import EditableValue
from tagalot.ui.main_window import MainWindow
from tests.ui.test_contents_search import _open
from tests.ui.test_file_extra_page import window

pytestmark = pytest.mark.gui

__all__ = ["window"]  # fixtures


def test_editing_a_books_series_on_its_page(qtbot: QtBot, window: MainWindow) -> None:
    session = window.session
    assert session is not None
    page = _open(qtbot, window, session, "Equal Rites")
    series = page.details.findChild(EditableValue, "field_series")
    assert isinstance(series, EditableValue)
    assert series.editable
    universe = page.details.findChild(EditableValue, "field_universe")
    assert isinstance(universe, EditableValue)
    assert not universe.editable  # it follows its series (in Discworld's universe)
    assert universe.toolTip() == "It comes from its series, Discworld: change it there."

    series.committed.emit("Witches")  # as typing a name and Enter does
    qtbot.waitUntil(
        lambda: (
            page.detail is not None
            and any(f.value == "Witches" for s in page.detail.sections for f in s.fields)
        ),
        timeout=5000,
    )
    assert window.statusBar().currentMessage() == "Set Series of 'Equal Rites' to 'Witches'."
    window.tag_actions.undo()
    qtbot.waitUntil(
        lambda: (
            page.detail is not None
            and any(f.value == "Discworld" for s in page.detail.sections for f in s.fields)
        ),
        timeout=5000,
    )
