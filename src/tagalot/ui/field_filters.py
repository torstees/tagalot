"""Field filters in the filter bar (DESIGN.md §12 "Filter bar").

```
[+ Field ▾]  →  Extension  →  ┌ Extension ─────────┐
                Width          │ ☑ .png        (6)  │
                Folder         │ ☑ .jpg        (3)  │
                               │ ☐ .ttf        (1)  │
                               │  [Clear] [Apply]   │
                               └────────────────────┘
(Extension: .png, .jpg x)  (Width: 500-1920 x)  (Folder contains "ice" x)
```

A field's filter depends on how it is searchable: **choice** fields pick values from those
among the current results (with counts), **range** fields take a From and a To, **text**
fields contain or start with some text. Applied filters are chips; clicking one edits it.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.search_spec import ChoiceFilter, FieldFilter, RangeFilter, TextFilter, TextMatch
from tagalot.ui.field_editor import ParseError, editor_text, parse_value
from tagalot.ui.models.results import display_value

CLOSE_MARK = "\u00d7"
DASH = "\u2013"
VISIBLE_CHOICES = 10
"""Values the choice list shows before it scrolls."""
CHOICES_SHOWN = 3
"""Values a choice chip names before "+N"."""

ChoiceCounts = list[tuple[Any, int]]
ChoiceLoader = Callable[[str, Callable[[ChoiceCounts], None]], None]
"""``load(field name, on_done)``: the page finds the field's values among its results."""


@dataclass(frozen=True)
class FilterField:
    """A field the current scope can be filtered on."""

    name: str
    label: str
    kind: str
    """``"text"``, ``"range"``, or ``"choice"``: the field's ``search``."""
    type: type = str


def describe(filter_: FieldFilter, label: str) -> str:
    """How a filter reads on its chip."""
    match filter_:
        case ChoiceFilter(values=values):
            shown = [display_value(v) for v in values[:CHOICES_SHOWN]]
            more = len(values) - CHOICES_SHOWN
            return f"{label}: {', '.join(shown)}" + (f" +{more}" if more > 0 else "")
        case RangeFilter(low=low, high=high):
            if low is not None and high is not None:
                return f"{label}: {display_value(low)}{DASH}{display_value(high)}"
            if low is not None:
                return f"{label} ≥ {display_value(low)}"
            return f"{label} ≤ {display_value(high)}"
        case TextFilter(text=text, match=match):
            how = "starts with" if match is TextMatch.STARTS_WITH else "contains"
            return f'{label} {how} "{text}"'


class FieldChip(QFrame):
    """``[Extension: .png, .jpg x]``: one field filter. Click to edit, the close button to
    remove."""

    removed = Signal(str)
    edit_requested = Signal(str)

    def __init__(self, field: FilterField, filter_: FieldFilter, parent: QWidget | None = None):
        super().__init__(parent)
        self.field = field
        self.filter = filter_
        self.setObjectName("field_chip")
        self.label = QLabel(describe(filter_, field.label))
        self.close_button = QToolButton()
        self.close_button.setText(CLOSE_MARK)
        self.close_button.setAutoRaise(True)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setToolTip("Remove this filter")
        self.close_button.clicked.connect(lambda: self.removed.emit(field.name))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 1, 2, 1)
        layout.setSpacing(2)
        layout.addWidget(self.label)
        layout.addWidget(self.close_button)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_available(True)

    def set_available(self, available: bool) -> None:
        """A filter on a field the listed types don't all have is kept but not applied."""
        self.available = available
        border = "rgba(142, 68, 173, 0.9)" if available else "rgba(128, 128, 128, 0.6)"
        fill = "rgba(142, 68, 173, 0.15)" if available else "transparent"
        line = "solid" if available else "dashed"
        self.setStyleSheet(
            f"#field_chip {{ background: {fill}; border: 1px {line} {border};"
            " border-radius: 11px; }"
        )
        self.setToolTip(
            "Click to change this filter"
            if available
            else "Not applied: not every type listed here has this field"
        )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.edit_requested.emit(self.field.name)
        super().mouseReleaseEvent(event)


class FieldFilterPopup(QFrame):
    """The editor for one field's filter. Emits :attr:`applied` with the new filter, or
    ``None`` to remove it."""

    applied = Signal(object)

    def __init__(
        self,
        field: FilterField,
        current: FieldFilter | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.field = field
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        heading = QLabel(field.label)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        self.error = QLabel()
        self.error.setStyleSheet("color: #c0392b;")
        self.error.hide()

        self.values: QListWidget | None = None
        self.low: QLineEdit | None = None
        self.high: QLineEdit | None = None
        self.text: QLineEdit | None = None
        self.match: QComboBox | None = None
        if field.kind == "choice":
            self.values = QListWidget()
            self.values.setMinimumWidth(220)
            self.values.addItem("Loading…")
            self._chosen = set(current.values) if isinstance(current, ChoiceFilter) else set()
            layout.addWidget(self.values)
        elif field.kind == "range":
            self.low, self.high = QLineEdit(), QLineEdit()
            if isinstance(current, RangeFilter):
                self.low.setText(editor_text(current.low))
                self.high.setText(editor_text(current.high))
            row = QHBoxLayout()
            for label, box in (("From", self.low), ("To", self.high)):
                box.setPlaceholderText("any")
                box.returnPressed.connect(self._apply)
                row.addWidget(QLabel(label))
                row.addWidget(box)
            layout.addLayout(row)
        else:
            self.match = QComboBox()
            self.match.addItem("contains", TextMatch.CONTAINS)
            self.match.addItem("starts with", TextMatch.STARTS_WITH)
            self.text = QLineEdit()
            self.text.setMinimumWidth(200)
            self.text.returnPressed.connect(self._apply)
            if isinstance(current, TextFilter):
                self.text.setText(current.text)
                self.match.setCurrentIndex(0 if current.match is TextMatch.CONTAINS else 1)
            row = QHBoxLayout()
            row.addWidget(self.match)
            row.addWidget(self.text, 1)
            layout.addLayout(row)
        layout.addWidget(self.error)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        clear = QPushButton("Clear")
        clear.setToolTip("Remove this filter")
        clear.clicked.connect(self._clear)
        apply = QPushButton("Apply")
        apply.setDefault(True)
        apply.clicked.connect(self._apply)
        buttons.addWidget(clear)
        buttons.addWidget(apply)
        layout.addLayout(buttons)

    def set_choices(self, counts: Sequence[tuple[Any, int]]) -> None:
        """The values to pick from (those among the results, with counts), plus any already
        chosen that no longer appear."""
        values = self.values
        if values is None:
            return
        values.clear()
        seen = set()
        for value, count in counts:
            seen.add(value)
            self._add_choice(value, f"{display_value(value)}  ({count:,})")
        for value in sorted(self._chosen - seen, key=str):
            self._add_choice(value, f"{display_value(value)}  (0)")
        if values.count() == 0:
            values.addItem("No values among these results")
        # As tall as its rows (up to ten, then it scrolls), not a fixed box.
        rows = min(values.count(), VISIBLE_CHOICES)
        values.setFixedHeight(rows * values.sizeHintForRow(0) + 2 * values.frameWidth() + 4)
        self.adjustSize()

    def _add_choice(self, value: Any, text: str) -> None:
        assert self.values is not None
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, value)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            Qt.CheckState.Checked if value in self._chosen else Qt.CheckState.Unchecked
        )
        self.values.addItem(item)

    def filter(self) -> FieldFilter | None:
        """The filter as currently set in the popup (``None``: nothing, remove it).
        Raises :class:`ParseError` for a range bound that doesn't parse."""
        name = self.field.name
        if self.values is not None:
            chosen = tuple(
                item.data(Qt.ItemDataRole.UserRole)
                for i in range(self.values.count())
                if (item := self.values.item(i)).checkState() == Qt.CheckState.Checked
            )
            return ChoiceFilter(name, chosen) if chosen else None
        if self.low is not None and self.high is not None:
            low = parse_value(self.low.text(), self.field.type)
            high = parse_value(self.high.text(), self.field.type)
            if low is None and high is None:
                return None
            return RangeFilter(name, low, high)
        assert self.text is not None
        assert self.match is not None
        text = self.text.text().strip()
        return TextFilter(name, text, self.match.currentData()) if text else None

    def _apply(self) -> None:
        try:
            chosen = self.filter()
        except ParseError as e:
            self.error.setText(str(e))
            self.error.show()
            return
        self.applied.emit(chosen)
        self.close()

    def _clear(self) -> None:
        self.applied.emit(None)
        self.close()

    def show_at(self, point: QPoint) -> None:
        self.adjustSize()
        self.move(point)
        self.show()
        focus = self.text or self.low or self.values
        if focus is not None:
            focus.setFocus()
