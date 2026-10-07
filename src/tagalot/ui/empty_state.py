"""The knight (#360): what a page shows when there's nothing to show, and the splash screen
while a keep opens from the command line.

:class:`EmptyState` is a heading and a line on what to do next, under the knight (or
without him, compact, for the small searches inside an item's page), with optional buttons.
"""

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplashScreen,
    QVBoxLayout,
    QWidget,
)

from tagalot.resources import knight_file

KNIGHT_SIZE = 140
"""The knight on an empty page (drawn from a 256 px picture, sharp on high-DPI screens)."""
SPLASH_SIZE = (320, 340)


def knight_pixmap(size: int, ratio: float = 1.0) -> QPixmap:
    """The knight at ``size`` logical pixels (an empty pixmap if the picture is missing)."""
    picture = QPixmap(str(knight_file()))
    if picture.isNull():
        return picture
    scaled = picture.scaled(
        round(size * ratio),
        round(size * ratio),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    scaled.setDevicePixelRatio(ratio)
    return scaled


class EmptyState(QWidget):
    """The knight, a heading, a hint, and buttons, centred. :meth:`show_text` changes the
    words; :meth:`set_compact` drops the knight (for small places)."""

    def __init__(self, title: str = "", hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.knight = QLabel()
        self.knight.setObjectName("knight")
        self.knight.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.knight.setPixmap(knight_pixmap(KNIGHT_SIZE, self.devicePixelRatioF()))
        self.title = QLabel()
        self.title.setObjectName("empty_title")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(font)
        self.hint = QLabel()
        self.hint.setObjectName("empty_hint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        muted = self.hint.palette()
        muted.setColor(
            QPalette.ColorRole.WindowText, muted.color(QPalette.ColorRole.PlaceholderText)
        )
        self.hint.setPalette(muted)
        self.buttons = QHBoxLayout()
        self.buttons.addStretch(1)
        self._button_count = 0

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 12, 24, 12)
        column.addStretch(1)
        column.addWidget(self.knight, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(self.title)
        column.addWidget(self.hint)  # full width, centred text: it wraps as it should
        column.addLayout(self.buttons)
        column.addStretch(2)
        self.show_text(title, hint)

    def show_text(self, title: str, hint: str = "") -> None:
        self.title.setText(title)
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))

    def set_compact(self, compact: bool) -> None:
        """Without the knight: for an item page's small searches."""
        self.knight.setVisible(not compact)

    def add_button(self, label: str, callback: Callable[[], object]) -> QPushButton:
        button = QPushButton(label)
        button.clicked.connect(lambda: callback())
        self.buttons.insertWidget(1 + self._button_count, button)
        if self._button_count == 0:
            self.buttons.addStretch(1)
        self._button_count += 1
        return button


def splash_screen(name: str) -> QSplashScreen:
    """The splash while ``tagalot <keep>`` opens a keep: the knight, "Tagalot", and
    "Opening <name>…". Close it with ``finish(window)`` (or ``close()``)."""
    width, height = SPLASH_SIZE
    ratio = 2.0  # drawn at twice the size: sharp on high-DPI screens, scaled down elsewhere
    canvas = QPixmap(round(width * ratio), round(height * ratio))
    canvas.setDevicePixelRatio(ratio)
    canvas.fill(QColor("#1d3b5c"))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    knight = knight_pixmap(200, ratio)
    painter.drawPixmap((width - 200) // 2, 28, knight)
    font = painter.font()
    font.setPointSize(20)
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(0, 236, width, 40, Qt.AlignmentFlag.AlignCenter, "Tagalot")
    painter.end()
    splash = QSplashScreen(canvas)
    # Not above everything: opening may ask first (upgrading the keep) or say why it can't.
    splash.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
    splash.showMessage(
        f"Opening {name}…",
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
        QColor("#d8e6f3"),
    )
    return splash
