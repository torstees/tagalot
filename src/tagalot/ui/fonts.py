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

from tagalot.core.fonts import is_font_file
from tagalot.core.keep import KeepConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FontLook:
    """The fonts a keep shows: families by place (None for the system's), and the text's
    size in percent of the system's."""

    titles: str | None = None
    notes: str | None = None
    text: str | None = None
    size: int = 100


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
    config: KeepConfig, files: Mapping[str, bytes | None]
) -> tuple[FontLook, dict[str, str]]:
    """The fonts a keep's configuration names, and the places whose font isn't there (by
    place, the value), which show the system's."""
    families: dict[str, str | None] = {}
    missing = {}
    for place, value in config.fonts.items():
        families[place] = family_of(value, files)
        if families[place] is None:
            missing[place] = value
    look = FontLook(
        titles=families.get("titles"),
        notes=families.get("notes"),
        text=families.get("text"),
        size=config.text_size or 100,
    )
    return look, missing


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
