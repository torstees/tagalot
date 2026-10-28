"""A selected item's text and icons get whichever color reads on the selection's fill as
the style draws it (#556 review): Windows 11 tints a selection faintly and keeps dark text in
light mode, but fills lists with alternating rows with the accent."""

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QTreeView
from pytestqt.qtbot import QtBot

from tagalot.ui.icons import lucide
from tagalot.ui.selection import contrast, selected_text_color

pytestmark = pytest.mark.gui

TEXT, HIGHLIGHTED = "#123456", "#fedcba"


def _palette(highlight: str) -> QPalette:
    palette = QPalette(QApplication.palette())
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(HIGHLIGHTED))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(highlight))
    return palette


@pytest.mark.parametrize(
    ("highlight", "expected"),
    [("#202060", HIGHLIGHTED), ("#e8e8ff", TEXT)],  # a strong fill; a faint one
)
def test_the_color_that_reads_on_the_fill(qtbot: QtBot, highlight: str, expected: str) -> None:
    view = QTreeView()
    qtbot.addWidget(view)
    view.setAlternatingRowColors(True)
    assert selected_text_color(_palette(highlight), view).name() == expected
    assert selected_text_color(_palette(highlight)).name() == expected  # no view: the app's


def test_highlighted_text_is_kept_while_it_reads(qtbot: QtBot) -> None:
    """White on Fusion's blue stays white, though black contrasts a little more there."""
    palette = _palette("#308cc6")
    palette.setColor(QPalette.ColorRole.Text, QColor("#000000"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    assert contrast(QColor("#000000"), QColor("#308cc6")) > contrast(
        QColor("#ffffff"), QColor("#308cc6")
    )
    assert selected_text_color(palette).name() == "#ffffff"


def test_selected_icons_use_it(qapp: QApplication) -> None:
    old = qapp.palette()
    try:
        for highlight, expected in (("#202060", HIGHLIGHTED), ("#e8e8ff", TEXT)):
            qapp.setPalette(_palette(highlight))
            image = lucide("film").pixmap(QSize(24, 24), QIcon.Mode.Selected).toImage()
            drawn = {
                image.pixelColor(x, y).name()
                for x in range(24)
                for y in range(24)
                if image.pixelColor(x, y).alpha() == 255
            }
            assert drawn == {expected}, highlight
    finally:
        qapp.setPalette(old)


def test_contrast() -> None:
    assert contrast(QColor("#000000"), QColor("#ffffff")) == pytest.approx(21)
    assert contrast(QColor("#777777"), QColor("#777777")) == pytest.approx(1)
