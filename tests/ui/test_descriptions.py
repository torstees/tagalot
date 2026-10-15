"""Descriptions (#414): a type's ``description_field`` under the selected item in the preview
strip, in grid cards' tooltips, and as a list column. On the books demo, where *Mother of
Learning* has a synopsis."""

import pytest
from pytestqt.qtbot import QtBot

from tagalot.ui.models.results import ResultColumn, cell_text, one_line, wrapped
from tagalot.ui.preview import THUMBNAIL, ElidedLabel
from tagalot.ui.search_view import SearchPage
from tests.ui.test_field_columns import _books, session
from tests.ui.test_preview import _select

pytestmark = pytest.mark.gui

__all__ = ["session"]  # fixtures

SYNOPSIS = "Zorian relives the same month at magic school, again and again."


def _row(page: SearchPage, title: str) -> int:
    for row in range(page.model.rowCount()):
        hit = page.model.hit(row)
        if hit is not None and hit.title == title:
            return row
    raise AssertionError(f"{title!r} isn't among the results")


def test_the_preview_strip_shows_the_description(qtbot: QtBot, session: object) -> None:
    page = _books(qtbot, session)  # type: ignore[arg-type]
    strip = page.preview
    _select(page, ["Mother of Learning"])
    qtbot.waitUntil(lambda: strip.description.full_text == SYNOPSIS, timeout=5000)
    assert strip.description.toolTip() == SYNOPSIS
    assert strip.height() == THUMBNAIL + 14  # the strip keeps its height

    _select(page, ["The Lighthouse Keeper"])  # no description
    qtbot.waitUntil(lambda: strip.title.text().startswith("The Lighthouse Keeper"), timeout=5000)
    assert strip.description.full_text == ""
    _select(page, [])
    qtbot.waitUntil(lambda: strip.description.full_text == "", timeout=5000)


def test_a_cards_tooltip_shows_the_description(qtbot: QtBot, session: object) -> None:
    page = _books(qtbot, session)  # type: ignore[arg-type]
    row = _row(page, "Mother of Learning")
    tooltip = page.grid.card_tooltip(row)
    assert tooltip is not None
    lines = tooltip.split("\n")
    assert lines[:3] == ["Mother of Learning", "Author: nobody103", "Series: Mother of Learning"]
    assert lines[3] == "Rating: ★★★★★  5 out of 5"  # stars and the number (#419)
    assert lines[4:] == ["", SYNOPSIS]  # set apart, and no blank line after it
    short = page.grid.card_tooltip(_row(page, "The Lighthouse Keeper"))
    assert short is not None
    assert "" not in short.split("\n")  # no description: no gap


def test_the_list_shows_the_description_on_one_line(qtbot: QtBot, session: object) -> None:
    page = _books(qtbot, session)  # type: ignore[arg-type]
    column = next(c for c in page.model.columns if c.key == "description")
    assert column.description
    shown = [c.key for i, c in enumerate(page.table.columns) if not page.table.isColumnHidden(i)]
    assert "description" in shown
    hit = page.model.hit(_row(page, "Mother of Learning"))
    assert hit is not None
    row = (hit, {"description": "Two lines\nof  text."})
    assert cell_text(column, row, {}) == "Two lines of text."
    plain = ResultColumn("description", "Description")
    assert cell_text(plain, row, {}) == "Two lines\nof  text."  # only the description


def test_a_long_description_is_cut() -> None:
    assert one_line(" A\n\nB   c ") == "A B c"
    text = " ".join(["word"] * 100)
    lines = wrapped(text, width=20, lines=3)
    assert len(lines) == 3
    assert all(len(line) <= 21 for line in lines)
    assert lines[-1].endswith("…")
    assert wrapped("Short.", width=20) == ["Short."]


def test_the_strips_description_is_elided(qtbot: QtBot) -> None:
    label = ElidedLabel()
    qtbot.addWidget(label)
    label.show()
    label.resize(120, 20)
    label.set_full_text(" ".join(["word"] * 50))
    assert label.text().endswith("…")
    assert label.fontMetrics().horizontalAdvance(label.text()) <= 120
    label.resize(4000, 20)
    assert label.text() == label.full_text
