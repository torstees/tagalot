"""A keep's colors (#558, DESIGN.md §12 *Colors*): the system's, always light, always dark,
or its own.

Light and dark keep Qt's native style and ask it for that scheme (``setColorScheme``). The
keep's own colors switch to Qt's Fusion style, since the native Windows 11 style ignores much
of a palette, and build a whole palette from five colors (:func:`custom_palette`).

Qt's palette and style belong to the application, not to a window, so the look follows the
keep whose window is active: applied when its window opens or becomes active, and when its
colors change. Applying the same look again does nothing, so switching windows of keeps that
look alike doesn't flicker.
"""

import logging
from collections.abc import Mapping

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from tagalot.core.keep import COLOR_ROLES, KeepConfig
from tagalot.ui.selection import contrast

logger = logging.getLogger(__name__)

FUSION = "fusion"
READABLE_TEXT = 4.5
"""The contrast below which Keep configuration warns that text is hard to read (WCAG AA)."""

_SCHEMES = {
    "system": Qt.ColorScheme.Unknown,
    "light": Qt.ColorScheme.Light,
    "dark": Qt.ColorScheme.Dark,
}

_native: str | None = None
"""The style Tagalot started with (Windows 11, macOS…), put back when a keep's own colors
aren't used."""
_applied: tuple[object, ...] = ("system",)
"""The look applied last; the start is the system's."""


def remember_native_style() -> None:
    """Note the application's own style, before any keep changes it."""
    global _native
    app = QApplication.instance()
    if _native is None and isinstance(app, QApplication):
        _native = app.style().name()


def apply_colors(mode: str, custom: Mapping[str, str] | None = None) -> bool:
    """Give the application a keep's look: ``mode`` from ``COLOR_MODES`` and its own colors.
    Returns whether anything changed."""
    global _applied
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return False
    remember_native_style()
    colors = dict(custom or {})
    key: tuple[object, ...] = (mode, *sorted(colors.items())) if mode == "custom" else (mode,)
    if key == _applied:
        return False
    hints = QGuiApplication.styleHints()
    if mode == "custom":
        palette = custom_palette(colors, app.palette())
        _set_style(app, FUSION)
        dark = palette.color(QPalette.ColorRole.Window).lightnessF() < 0.5
        hints.setColorScheme(Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light)
        app.setPalette(palette)
    else:
        _set_style(app, _native or app.style().name())
        app.setPalette(QPalette())  # the platform's again
        hints.setColorScheme(_SCHEMES.get(mode, Qt.ColorScheme.Unknown))
    _applied = key
    _repolish(app)
    logger.debug("Applied the colors %s", key)
    return True


def _repolish(app: QApplication) -> None:
    """Style sheets naming palette colors (``palette(placeholder-text)``) keep the colors
    they were set with: set them again, so dimmed text follows the new palette."""
    for widget in app.allWidgets():
        sheet = widget.styleSheet()
        if sheet:
            widget.setStyleSheet("")
            widget.setStyleSheet(sheet)


def apply_keep_colors(config: KeepConfig) -> bool:
    """Give the application ``config``'s look (see :func:`apply_colors`)."""
    return apply_colors(config.colors, config.custom_colors)


def reset_colors() -> None:
    """The system's look again (tests, and the launcher)."""
    apply_colors("system")


def _set_style(app: QApplication, name: str) -> None:
    if app.style().name().lower() == name.lower():
        return
    app.setStyle(name)
    from tagalot.ui.hover import restyle_views  # the results lists keep their own proxy

    restyle_views()


def starting_colors(palette: QPalette | None = None) -> dict[str, str]:
    """The keep's own colors to start from: the colors shown now."""
    palette = palette or QApplication.palette()
    roles = {
        "background": QPalette.ColorRole.Window,
        "text": QPalette.ColorRole.Text,
        "panels": QPalette.ColorRole.Base,
        "accent": QPalette.ColorRole.Highlight,
        "dimmed": QPalette.ColorRole.PlaceholderText,
    }
    return {name: palette.color(role).name() for name, role in roles.items()}


def mix(a: QColor, b: QColor, amount: float) -> QColor:
    """``a`` moved ``amount`` (0 to 1) of the way to ``b``."""
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * amount,
        a.greenF() + (b.greenF() - a.greenF()) * amount,
        a.blueF() + (b.blueF() - a.blueF()) * amount,
    )


def custom_palette(colors: Mapping[str, str], base: QPalette | None = None) -> QPalette:
    """A whole palette from a keep's five colors (``COLOR_ROLES``); a color not given comes
    from ``base`` (the palette shown now)."""
    given = {**starting_colors(base or QApplication.palette()), **colors}
    background, text = QColor(given["background"]), QColor(given["text"])
    panels, accent = QColor(given["panels"]), QColor(given["accent"])
    dimmed = QColor(given["dimmed"])
    dark = background.lightnessF() < 0.5
    black, white = QColor("#000000"), QColor("#ffffff")
    on_accent = white if contrast(white, accent) >= contrast(black, accent) else black
    link = accent if contrast(accent, panels) >= 3.0 else mix(accent, text, 0.5)

    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: background,
        QPalette.ColorRole.WindowText: text,
        QPalette.ColorRole.Base: panels,
        QPalette.ColorRole.AlternateBase: mix(panels, text, 0.05),
        QPalette.ColorRole.ToolTipBase: panels,
        QPalette.ColorRole.ToolTipText: text,
        QPalette.ColorRole.PlaceholderText: dimmed,
        QPalette.ColorRole.Text: text,
        QPalette.ColorRole.Button: mix(background, text, 0.08),
        QPalette.ColorRole.ButtonText: text,
        QPalette.ColorRole.BrightText: white if dark else black,
        QPalette.ColorRole.Light: mix(background, white, 0.3 if dark else 0.6),
        QPalette.ColorRole.Midlight: mix(background, white, 0.15 if dark else 0.3),
        QPalette.ColorRole.Mid: mix(background, text, 0.25),
        QPalette.ColorRole.Dark: mix(background, black, 0.45 if dark else 0.3),
        QPalette.ColorRole.Shadow: black,
        QPalette.ColorRole.Highlight: accent,
        QPalette.ColorRole.HighlightedText: on_accent,
        QPalette.ColorRole.Link: link,
        QPalette.ColorRole.LinkVisited: link,
        QPalette.ColorRole.Accent: accent,
    }
    for role, value in roles.items():
        palette.setColor(role, value)
    faded = mix(dimmed, background, 0.35)
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.PlaceholderText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, faded)
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, mix(accent, background, 0.5)
    )
    return palette


def hard_to_read(colors: Mapping[str, str]) -> list[str]:
    """What reads poorly in a keep's own colors (text against the background and against
    lists and panels, below :data:`READABLE_TEXT`), as sentences for Keep configuration."""
    given = {role: QColor(colors[role]) for role in COLOR_ROLES if role in colors}
    problems = []
    for ground, label in (("background", "the background"), ("panels", "lists and panels")):
        if "text" in given and ground in given:
            ratio = contrast(given["text"], given[ground])
            if ratio < READABLE_TEXT:
                problems.append(
                    f"Text on {label} is hard to read (contrast {ratio:.1f}:1; "
                    f"{READABLE_TEXT:g}:1 or more reads easily)."
                )
    return problems
