"""A rating on an item's page (#419): five stars, set by clicking a star (its left half
for a half star), cleared by clicking the rating it has."""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from tagalot.ui.field_editor import EditableValue
from tagalot.ui.stars import STAR_GAP, STAR_SIZE, StarRating

pytestmark = pytest.mark.gui


def _rating(qtbot: QtBot, value: float | None, scale: int = 5) -> EditableValue:
    widget = EditableValue(value, float, editable=True, label="Rating", display=f"stars/{scale}")
    qtbot.addWidget(widget)
    return widget


def test_a_rating_shows_as_stars(qtbot: QtBot) -> None:
    widget = _rating(qtbot, 3.5)
    assert widget.stars is not None
    assert widget.stars.halves == 7
    assert widget.stars.toolTip().startswith("3.5 out of 5")
    assert not widget.stack.isVisibleTo(widget)  # no text box: stars instead
    widget.start_editing()
    assert not widget.editing()
    movie = _rating(qtbot, 7.3, scale=10)  # a scraper's score out of 10
    assert movie.stars is not None
    assert movie.stars.halves == 7
    unrated = _rating(qtbot, None)
    assert unrated.stars is not None
    assert unrated.stars.halves == 0
    assert unrated.stars.toolTip().startswith("Not rated")


def test_clicking_a_star_rates_and_clicking_again_clears(qtbot: QtBot) -> None:
    widget = _rating(qtbot, None)
    stars = widget.stars
    assert stars is not None
    got: list[object] = []
    widget.committed.connect(got.append)
    third = stars.star_rect(2)
    left = QPoint(int(third.left()) + 2, int(third.center().y()))
    right = QPoint(int(third.right()) - 2, int(third.center().y()))
    QTest.mouseClick(stars, Qt.MouseButton.LeftButton, pos=right)
    QTest.mouseClick(stars, Qt.MouseButton.LeftButton, pos=left)
    assert got == [3.0, 2.5]  # the whole third star, then its left half
    widget.show_value(2.5, edited=True)  # as the page shows it again
    QTest.mouseClick(stars, Qt.MouseButton.LeftButton, pos=left)
    assert got[-1] is None  # the rating it had: cleared


def test_out_of_ten_a_star_is_two_points(qtbot: QtBot) -> None:
    widget = _rating(qtbot, None, scale=10)
    got: list[object] = []
    widget.committed.connect(got.append)
    assert widget.stars is not None
    widget.stars.chosen.emit(7)
    assert got == [7.0]


def test_read_only_stars_ignore_clicks(qtbot: QtBot) -> None:
    widget = EditableValue(4.0, float, editable=False, label="Rating", display="stars/5")
    qtbot.addWidget(widget)
    got: list[object] = []
    widget.committed.connect(got.append)
    assert widget.stars is not None
    QTest.mouseClick(widget.stars, Qt.MouseButton.LeftButton, pos=QPoint(2, 9))
    assert got == []


def test_where_a_click_lands(qtbot: QtBot) -> None:
    stars = StarRating(editable=True)
    qtbot.addWidget(stars)
    assert stars.halves_at(1) == 1
    assert stars.halves_at(STAR_SIZE - 1) == 2
    assert stars.halves_at(4 * (STAR_SIZE + STAR_GAP) + STAR_SIZE - 1) == 10
    assert stars.halves_at(5 * (STAR_SIZE + STAR_GAP) + 5) is None
