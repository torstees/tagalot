"""Named colors for meanings the palette has no role for (#558, DESIGN.md §12 *Colors*):
an error, a warning or drop outline, a rating's stars, a difference between items. Each
has a variant for light backgrounds and one for dark, chosen from the palette's window
color, so it reads on the system's colors and on any keep's own.
"""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ERROR = "error"
WARNING = "warning"
STAR = "star"
DIFFERS = "differs"

_ROLES: dict[str, tuple[str, str]] = {
    ERROR: ("#b0322a", "#ff7b6e"),
    WARNING: ("#b86e00", "#f0a020"),
    STAR: ("#d49a00", "#f2b01e"),
    DIFFERS: ("#ffbe0046", "#ffbe0046"),  # a translucent amber over a cell, either way
}
"""Each role's color on a light background, then on a dark one (``#rrggbb`` or
``#rrggbbaa``)."""


def is_dark(palette: QPalette | None = None) -> bool:
    """Whether the window's background is dark."""
    palette = palette or QApplication.palette()
    return palette.color(QPalette.ColorRole.Window).lightnessF() < 0.5


def color(role: str, palette: QPalette | None = None) -> QColor:
    """The role's color for ``palette`` (the application's by default)."""
    light, dark = _ROLES[role]
    value = dark if is_dark(palette) else light
    if len(value) == 9:  # with alpha
        result = QColor(value[:7])
        result.setAlpha(int(value[7:], 16))
        return result
    return QColor(value)


def css(role: str, palette: QPalette | None = None) -> str:
    """The role's color for a style sheet or rich text (``#rrggbb``)."""
    return color(role, palette).name()


def dimmed_css(palette: QPalette | None = None) -> str:
    """Dimmed text's color (the palette's placeholder text) for rich text."""
    palette = palette or QApplication.palette()
    return palette.color(QPalette.ColorRole.PlaceholderText).name()
