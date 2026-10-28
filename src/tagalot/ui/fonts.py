"""A keep's fonts (#565, DESIGN.md §12 *Fonts*): titles, notes, and text, and a text size.

As with its colors (``ui/appearance.py``), Qt's font belongs to the application, so the
fonts follow the keep whose window is active: the text's family and size are the
application's font, and titles and notes are asked for where they're drawn
(:func:`place_font`). Widgets with a font of their own (a page's title, the navigation's
headings) follow changes with :func:`follow`, and others listen to :func:`signals`.

A font file in the keep folder is read by the session (``core/fonts.py``) and registered
here; a family that isn't installed, or a file that's gone, gives the system's font.
"""

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QObject, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget

from tagalot.core.fonts import (
    GENERIC_FAMILIES,
    KEEP_FONTS,
    SYSTEM_FONT,
    family_folder,
    is_font_file,
    is_theme_font_file,
)
from tagalot.core.keep import FONT_PLACES, KeepConfig
from tagalot.themes.api import Theme

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FontLook:
    """The fonts a keep shows: families by place (None for the system's), and the text's
    size in percent of the system's."""

    titles: str | None = None
    notes: str | None = None
    text: str | None = None
    size: int = 100


@dataclass(frozen=True)
class PlaceFont:
    """A place's font and where it comes from (Keep configuration says so)."""

    family: str | None
    """The family shown; None for the system's."""
    origin: str
    """``"keep"`` (the keep's choice), ``"theme"`` (its suggestion), or ``"system"``."""
    value: str | None = None
    """The keep's value, or the theme's entry that shows."""
    missing: tuple[str, ...] = ()
    """What was named but isn't here: the keep's value, or the theme's fonts passed over."""


_HINTS = {
    "serif": QFont.StyleHint.Serif,
    "sans-serif": QFont.StyleHint.SansSerif,
    "monospace": QFont.StyleHint.Monospace,
    "cursive": QFont.StyleHint.Cursive,
    "fantasy": QFont.StyleHint.Fantasy,
}


class _Signals(QObject):
    changed = Signal()
    """The fonts changed: widgets drawing titles or notes draw them again."""


_signals: _Signals | None = None
_system: QFont | None = None
"""The application's font before any keep changed it."""
_look = FontLook()
_registered: dict[bytes, str | None] = {}
"""Families of font files registered with Qt, by their bytes' digest."""


def signals() -> _Signals:
    global _signals
    if _signals is None:
        _signals = _Signals()
    return _signals


def remember_system_font() -> None:
    """Note the application's own font, before any keep changes it."""
    global _system
    if _system is None:
        _system = QFont(QApplication.font())


def current() -> FontLook:
    """The fonts shown now."""
    return _look


def register_font(data: bytes) -> str | None:
    """Register a font file's bytes with Qt (once) and return its family; None when Qt
    can't read it."""
    digest = hashlib.blake2b(data).digest()
    if digest not in _registered:
        font_id = QFontDatabase.addApplicationFontFromData(QByteArray(data))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        _registered[digest] = families[0] if families else None
        if not families:
            logger.warning("A keep's font file isn't a font Qt can read")
    return _registered[digest]


def installed(family: str) -> bool:
    """Whether a family is installed (or registered), case aside."""
    wanted = family.casefold()
    return any(f.casefold() == wanted for f in QFontDatabase.families())


def family_of(value: str, files: Mapping[str, bytes | None]) -> str | None:
    """The family a ``[appearance.fonts]`` value shows: a registered file's, or an
    installed family; None when neither is there."""
    if is_font_file(value):
        data = files.get(value)
        return register_font(data) if data is not None else None
    return value if installed(value) else None


def look_of(
    config: KeepConfig, files: Mapping[str, bytes | None], theme: type[Theme] | None = None
) -> tuple[FontLook, dict[str, PlaceFont]]:
    """The fonts a keep shows, and where each place's comes from: the keep's choice
    (``[appearance.fonts]``), else the theme's suggestion (#567), else the system's. A
    font the keep names that isn't there shows the system's."""
    for data in files.values():  # a family's bold and italic too (#566)
        if data is not None:
            register_font(data)
    folders = _folder_families(files)
    suggested = getattr(theme, "fonts", {}) if theme is not None else {}
    places = {}
    for place in FONT_PLACES:
        value = config.fonts.get(place)
        if value == SYSTEM_FONT:
            places[place] = PlaceFont(None, "keep", value)
        elif value is not None:
            family = family_of(value, files)
            places[place] = PlaceFont(family, "keep", value, () if family else (value,))
        else:
            places[place] = _suggested(tuple(suggested.get(place, ())), files, folders)
    size = config.text_size or getattr(theme, "text_size", None) or 100
    look = FontLook(
        titles=places["titles"].family,
        notes=places["notes"].family,
        text=places["text"].family,
        size=size,
    )
    return look, places


def _folder_families(files: Mapping[str, bytes | None]) -> dict[str, str]:
    """The families of the keep's downloaded fonts, by their folder's name, case-folded:
    Qt may name a family otherwise (a variable font's default instance, "Literata 12pt")."""
    families: dict[str, str] = {}
    for key, data in sorted(files.items()):
        parts = key.split("/")
        if len(parts) == 3 and parts[0] == KEEP_FONTS and data is not None:
            family = register_font(data)
            if family and parts[1].casefold() not in families:
                families[parts[1].casefold()] = family
    return families


def _suggested(
    chain: tuple[str, ...], files: Mapping[str, bytes | None], folders: Mapping[str, str]
) -> PlaceFont:
    """The first font of a theme's chain that's here: a file beside the theme, a family
    downloaded into the keep, an installed family, or a generic family's font."""
    skipped: list[str] = []
    for entry in chain:
        if entry in GENERIC_FAMILIES:
            hinted = QFont()
            hinted.setStyleHint(_HINTS.get(entry, QFont.StyleHint.AnyStyle))
            return PlaceFont(hinted.defaultFamily(), "theme", entry, tuple(skipped))
        family: str | None
        if is_theme_font_file(entry):
            data = files.get(f"theme:{entry}")
            family = register_font(data) if data is not None else None
        else:
            family = folders.get(family_folder(entry).casefold())
            if family is None and installed(entry):
                family = entry
        if family is not None:
            return PlaceFont(family, "theme", entry, tuple(skipped))
        skipped.append(entry)
    return PlaceFont(None, "theme" if chain else "system", None, tuple(skipped))


def apply_fonts(look: FontLook) -> bool:
    """Give the application a keep's fonts. Returns whether anything changed."""
    global _look
    app = QApplication.instance()
    if not isinstance(app, QApplication) or look == _look:
        return False
    remember_system_font()
    assert _system is not None
    font = QFont(_system)
    if look.text:
        font.setFamilies([look.text])
    if look.size != 100:
        if _system.pointSizeF() > 0:
            font.setPointSizeF(_system.pointSizeF() * look.size / 100)
        else:
            font.setPixelSize(max(round(_system.pixelSize() * look.size / 100), 6))
    _look = look
    app.setFont(font)
    signals().changed.emit()
    logger.debug("Applied the fonts %s", look)
    return True


def reset_fonts() -> None:
    """The system's fonts again (tests, and the launcher)."""
    apply_fonts(FontLook())


def place_font(
    place: str,
    *,
    bold: bool = False,
    larger: float = 0.0,
    scale: float = 1.0,
    base: QFont | None = None,
) -> QFont:
    """The font of a place (``"titles"``, ``"notes"``, ``"text"``): the application's
    (or ``base``), in the place's family, bold, ``larger`` points, or ``scale`` times as
    big, as asked."""
    font = QFont(base if base is not None else QApplication.font())
    family = getattr(_look, place, None)
    if family:
        font.setFamilies([family])
    if bold:
        font.setBold(True)
    if (larger or scale != 1.0) and font.pointSizeF() > 0:
        font.setPointSizeF(max(font.pointSizeF() * scale + larger, 6))
    return font


class _Follower(QObject):
    """Sets a widget's font now and whenever the fonts change; a child of the widget, so
    it goes with it."""

    def __init__(self, widget: QWidget, make: Callable[[], QFont]) -> None:
        super().__init__(widget)
        self._widget = widget
        self._make = make
        signals().changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self._widget.setFont(self._make())


def follow(widget: QWidget, make: Callable[[], QFont]) -> None:
    """Give ``widget`` the font ``make`` returns, now and after every change of fonts."""
    _Follower(widget, make)
