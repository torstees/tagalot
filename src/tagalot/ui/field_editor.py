"""Editing one value in place on a detail page (DESIGN.md §12 "Detail page").

```
Year:  1995  • edited  ✎      (hover shows the pencil; double-click or ✎ edits)
Year:  [1995        ]         (Enter or clicking away saves; Esc cancels)
```

:class:`EditableValue` shows a value and, when it may be edited, turns into a text box on
double-click (or the pencil). What the user types is parsed for the field's type; a value
that doesn't parse stays in the box with the reason, and a good one is reported with
:attr:`EditableValue.committed`. Yes/no fields are a checkbox that saves when ticked.
"""

import html
from datetime import UTC, date, datetime
from typing import Any

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices,
    QEnterEvent,
    QFocusEvent,
    QKeyEvent,
    QMouseEvent,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QStackedWidget,
    QToolButton,
    QWidget,
)

from tagalot.core.formats import is_web_address
from tagalot.ui.models.results import display_value

PENCIL = "✎"
BOX_WIDTH = 280
BOX_PADDING = 24
"""Room in the text box beside the value's text: its frame, margins, and the cursor."""
"""The text box's minimum width while editing."""
EDITED_TIP = "You edited this; scans won't change it (Undo puts it back)"


class ParseError(ValueError):
    """What the user typed doesn't fit the field; the message says what would."""


def parse_value(text: str, kind: type) -> Any:
    """The typed value for what the user typed (``None`` for nothing)."""
    text = text.strip()
    if not text:
        return None
    try:
        if kind is int:
            return int(text.replace(",", ""))
        if kind is float:
            return float(text.replace(",", ""))
        if kind is date:
            return date.fromisoformat(text)
        if kind is datetime:
            return datetime.fromisoformat(text)  # without a zone: local time
    except ValueError:
        raise ParseError(_expected(kind)) from None
    return text


def editor_text(value: Any) -> str:
    """How a value reads in the text box (the form :func:`parse_value` reads back)."""
    match value:
        case None:
            return ""
        case datetime():
            return value.astimezone().strftime("%Y-%m-%d %H:%M")
        case date():
            return value.isoformat()
        case _:
            return str(value)


def _expected(kind: type) -> str:
    return {
        int: "Type a whole number",
        float: "Type a number",
        date: "Type a date as YYYY-MM-DD",
        datetime: "Type a date and time as YYYY-MM-DD HH:MM",
    }.get(kind, "Type a value")


class _Box(QLineEdit):
    """The text box: Enter saves, Esc cancels, clicking away saves."""

    submitted = Signal()
    cancelled = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.submitted.emit()
        elif event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self.submitted.emit()


class _Stack(QStackedWidget):
    """A stack as big as the page it shows (not its biggest page), so what follows the
    value sits right after it."""

    def __init__(self) -> None:
        super().__init__()
        self.currentChanged.connect(lambda _index: self.updateGeometry())

    def sizeHint(self) -> QSize:
        current = self.currentWidget()
        if isinstance(current, QLabel):
            # A wrapping label guesses a narrow width; ask for the text's own width.
            width = current.fontMetrics().horizontalAdvance(current.text()) + 4
            return QSize(width, current.heightForWidth(width))
        if isinstance(current, QLineEdit):
            # The box's own hint is narrow (about 108 px), less than its minimum width, so a
            # stretch after the value squeezed it and clipped what was typed (#380 review):
            # ask for room for the whole value, and never less than the box's minimum.
            text = current.fontMetrics().horizontalAdvance(current.text()) + BOX_PADDING
            return QSize(max(current.minimumWidth(), text), current.sizeHint().height())
        return current.sizeHint() if current is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:
        current = self.currentWidget()
        if isinstance(current, QLabel):  # narrow windows: wrap rather than widen the page
            return QSize(min(self.sizeHint().width(), 120), current.minimumSizeHint().height())
        if isinstance(current, QLineEdit):
            return QSize(current.minimumWidth(), current.minimumSizeHint().height())
        return current.minimumSizeHint() if current is not None else super().minimumSizeHint()


def open_web_address(url: str) -> None:
    """Open an ``http(s)`` address in the browser (a ``"url"`` field's link). Tagalot itself
    never fetches it."""
    QDesktopServices.openUrl(QUrl(url))


class EditableValue(QWidget):
    """One value: shown, and editable in place when ``editable``. Emits :attr:`committed`
    with the new (typed) value."""

    committed = Signal(object)

    def __init__(
        self,
        value: Any,
        kind: type = str,
        *,
        editable: bool = False,
        edited: bool = False,
        label: str = "value",
        display: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.display = display
        self.value = value
        self.kind = kind
        self.editable = editable
        self.field_label = label
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.check: QCheckBox | None = None
        self.stack = _Stack()
        self.label = QLabel()
        self.label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self.label.setWordWrap(True)
        # Looked up when clicked, so tests can stand in for the browser.
        self.label.linkActivated.connect(lambda url: open_web_address(url))
        self.box = _Box()
        self.box.setMinimumWidth(BOX_WIDTH)
        self.box.submitted.connect(self._submit)
        self.box.cancelled.connect(self.cancel)
        self.stack.addWidget(self.label)
        self.stack.addWidget(self.box)
        if kind is bool:
            self.check = QCheckBox()
            self.check.setEnabled(editable)
            self.check.toggled.connect(self._toggled)
            layout.addWidget(self.check)
        else:
            layout.addWidget(self.stack)

        self.marker = QLabel("• edited")
        self.marker.setStyleSheet("color: palette(placeholder-text);")
        self.marker.setToolTip(EDITED_TIP)
        self.pencil = QToolButton()
        self.pencil.setText(PENCIL)
        self.pencil.setAutoRaise(True)
        self.pencil.setToolTip(f"Edit {label} (or double-click it)")
        self.pencil.clicked.connect(self.start_editing)
        self.pencil.setVisible(False)
        layout.addWidget(self.marker)
        layout.addWidget(self.pencil)
        layout.addStretch(1)  # the mark and pencil follow the value, not the far edge
        self.label.installEventFilter(self)
        self.show_value(value, edited)

    # --- showing ---

    def show_value(self, value: Any, edited: bool) -> None:
        """Show ``value`` (after a save, or a reload), leaving edit mode."""
        self.value = value
        self._show_text(value)
        self.stack.updateGeometry()
        self.marker.setVisible(edited)
        if self.check is not None:
            blocked = self.check.blockSignals(True)
            self.check.setChecked(bool(value))
            self.check.blockSignals(blocked)
        self.stack.setCurrentWidget(self.label)
        self.box.setStyleSheet("")
        self.box.setToolTip("")

    def _show_text(self, value: Any) -> None:
        """The value as text, or, for a ``"url"`` field holding a web address, as a link that
        opens it in the browser (double-click still edits it)."""
        if self.display == "url" and is_web_address(value):
            address = html.escape(value.strip(), quote=True)
            self.label.setTextFormat(Qt.TextFormat.RichText)
            self.label.setText(f'<a href="{address}">{address}</a>')
            self.label.setToolTip(f"Open {value.strip()} in your browser")
        else:
            self.label.setTextFormat(Qt.TextFormat.PlainText)
            self.label.setText(display_value(value, self.display))
            self.label.setToolTip("")

    def editing(self) -> bool:
        """Whether the text box is showing."""
        return self.stack.currentWidget() is self.box

    # --- editing ---

    def start_editing(self) -> None:
        if not self.editable or self.check is not None:
            return
        self.box.setText(editor_text(self.value))
        self.box.setPlaceholderText(_expected(self.kind) if self.kind is not str else "")
        self.stack.setCurrentWidget(self.box)
        self.box.setFocus(Qt.FocusReason.OtherFocusReason)
        self.box.selectAll()

    def cancel(self) -> None:
        self.stack.setCurrentWidget(self.label)
        self.box.setStyleSheet("")

    def _submit(self) -> None:
        if not self.editing():
            return
        try:
            value = parse_value(self.box.text(), self.kind)
        except ParseError as e:
            self.box.setStyleSheet("border: 1px solid #c0392b;")
            self.box.setToolTip(str(e))
            self.box.setPlaceholderText(str(e))
            return
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.astimezone(UTC)  # local time, compared as stored
        self.stack.setCurrentWidget(self.label)
        if value != self.value:
            self._show_text(value)  # at once; the page reloads
            self.committed.emit(value)

    def _toggled(self, on: bool) -> None:
        if on != bool(self.value):
            self.committed.emit(on)

    # --- hover and double-click ---

    def enterEvent(self, event: QEnterEvent) -> None:
        self.pencil.setVisible(self.editable and self.check is None and not self.editing())
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self.pencil.setVisible(False)
        super().leaveEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.label
            and event.type() == QEvent.Type.MouseButtonDblClick
            and isinstance(event, QMouseEvent)
        ):
            self.start_editing()
            return True
        return super().eventFilter(watched, event)
