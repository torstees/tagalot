"""The search view's filter bar: a text box, a tag box, and include / "but not" chips (§12).

```
🔍 [glacier            ]  🏷 [Add tag…      ]
[Iceland x] [Beach x] [not: Christmas x]     Clear all
```

Typing in the tag box lists matching tags (names and aliases, best first). Enter adds the
highlighted tag as an include chip; Shift+Enter adds it as a "but not" chip, the same keys
the tagging panel uses to apply and remove tags. The text box applies after a short pause
or on Enter. Every change emits :attr:`FilterBar.changed` with the current :class:`Filters`.
"""

from dataclasses import dataclass

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QFocusEvent,
    QKeyEvent,
    QKeySequence,
    QShortcut,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QListView,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from tagalot.core.tags import PATH_SEPARATOR, TagTree

TEXT_DELAY_MS = 300
"""How long the text box waits after the last keystroke before searching."""

SUGGESTION_LIMIT = 12

_TAG_ID = Qt.ItemDataRole.UserRole + 1

CLOSE_MARK = chr(0x00D7)  # MULTIPLICATION SIGN, the usual "remove" mark on a chip


@dataclass(frozen=True)
class Filters:
    """What the filter bar currently asks for: tag ids to include (each must match, with
    its descendants), tag ids to exclude, the search text, and ``only``, a type id the
    global search is narrowed to ("Show all" on one of its sections)."""

    include: tuple[int, ...] = ()
    exclude: tuple[int, ...] = ()
    text: str = ""
    only: str | None = None


class FlowLayout(QLayout):
    """Lays widgets out left to right, wrapping onto new lines (Qt's flow layout example)."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def insert_widget(self, index: int, widget: QWidget) -> None:
        """Insert ``widget`` before the item at ``index``."""
        self.addChildWidget(widget)
        self._items.insert(index, QWidgetItem(widget))
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        x, y, line_height = rect.x(), rect.y(), 0
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right() + 1:
                x, y, line_height = rect.x(), y + line_height + self._spacing, 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


class Chip(QFrame):
    """A removable tag chip: ``[Iceland x]`` or, for an excluded tag, ``[not: Christmas x]``."""

    removed = Signal(int)

    def __init__(self, tag_id: int, exclude: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tag_id = tag_id
        self.exclude = exclude
        self.setObjectName("exclude_chip" if exclude else "include_chip")
        self.label = QLabel()
        self.close_button = QToolButton()
        self.close_button.setText(CLOSE_MARK)
        self.close_button.setAutoRaise(True)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(lambda: self.removed.emit(self.tag_id))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 1, 2, 1)
        layout.setSpacing(2)
        layout.addWidget(self.label)
        layout.addWidget(self.close_button)
        # Translucent fills read on light and dark palettes alike.
        color = "220, 53, 69" if exclude else "13, 110, 253"
        self.setStyleSheet(
            f"#{self.objectName()} {{ background: rgba({color}, 0.16);"
            f" border: 1px solid rgba({color}, 0.7); border-radius: 11px; }}"
        )

    def set_tree(self, tree: TagTree | None) -> None:
        """Show the tag's name (its path if the name is ambiguous) from ``tree``."""
        if tree is not None and self.tag_id in tree:
            name = tree.display_name(self.tag_id)
            path = PATH_SEPARATOR.join(tree.path(self.tag_id))
        else:
            name = path = "(deleted tag)" if tree is not None else "…"
        self.label.setText(f"not: {name}" if self.exclude else name)
        if self.exclude:
            self.setToolTip(f"Hide items tagged {path}, or with any tag under it")
        else:
            self.setToolTip(f"Only items tagged {path}, or with any tag under it")
        self.close_button.setToolTip("Remove this filter")


class ScopeChip(QFrame):
    """``[Only: Album x]``: the global search narrowed to one type."""

    removed = Signal()

    def __init__(self, type_id: str, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.type_id = type_id
        self.setObjectName("scope_chip")
        self.label = QLabel(f"Only: {label}")
        self.close_button = QToolButton()
        self.close_button.setText(CLOSE_MARK)
        self.close_button.setAutoRaise(True)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setToolTip("Show every type again")
        self.close_button.clicked.connect(self.removed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 1, 2, 1)
        layout.setSpacing(2)
        layout.addWidget(self.label)
        layout.addWidget(self.close_button)
        self.setToolTip(f"Only {label} results are listed")
        self.setStyleSheet(
            "#scope_chip { background: rgba(128, 128, 128, 0.18);"
            " border: 1px solid rgba(128, 128, 128, 0.8); border-radius: 11px; }"
        )


class TagBox(QLineEdit):
    """A line edit that suggests tags as you type and emits :attr:`picked` with the chosen
    tag id and whether it is to be excluded (Shift held)."""

    picked = Signal(int, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tree: TagTree | None = None
        self.setPlaceholderText("Add tag…")
        self.setClearButtonEnabled(True)
        self.setToolTip("Type a tag name. Enter: only items with this tag. Shift+Enter: but not.")
        self.suggestions = QStandardItemModel(self)
        # A child of the window, not a popup window: it never takes focus from the box.
        self.popup = QListView()
        self.popup.setModel(self.suggestions)
        self.popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.popup.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.popup.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.popup.setUniformItemSizes(True)
        self.popup.setFrameShape(QFrame.Shape.StyledPanel)
        self.popup.hide()
        self.popup.clicked.connect(self._clicked)
        self.textEdited.connect(self._update)
        # Owned by the box, so it can't fire after the box is gone.
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(150)
        self._hide_timer.timeout.connect(self._hide_unless_focused)

    def set_tree(self, tree: TagTree | None) -> None:
        self.tree = tree
        if self.popup.isVisible():
            self._update(self.text())

    def popup_visible(self) -> bool:
        return self.popup.isVisible()

    def suggestion_ids(self) -> list[int]:
        """The tag ids currently suggested, in order."""
        ids = []
        for row in range(self.suggestions.rowCount()):
            tag_id = self.suggestions.item(row).data(_TAG_ID)
            if tag_id is not None:
                ids.append(int(tag_id))
        return ids

    def _update(self, text: str) -> None:
        self.suggestions.clear()
        if not text.strip():
            self.popup.hide()
            return
        tree = self.tree
        suggestions = tree.suggest(text, SUGGESTION_LIMIT) if tree is not None else []
        for suggestion in suggestions:
            assert tree is not None
            node = tree.node(suggestion.tag_id)
            label = node.name
            if suggestion.alias is not None:
                label += f"  ({suggestion.alias})"
            parents = tree.path(suggestion.tag_id)[:-1]
            if parents:
                label += f"   ·   {PATH_SEPARATOR.join(parents)}"
            item = QStandardItem(label)
            item.setData(suggestion.tag_id, _TAG_ID)
            item.setToolTip(PATH_SEPARATOR.join(tree.path(suggestion.tag_id)))
            self.suggestions.appendRow(item)
        if not suggestions:
            empty = QStandardItem("Loading tags…" if tree is None else "No matching tags")
            empty.setEnabled(False)
            self.suggestions.appendRow(empty)
        else:
            self.popup.setCurrentIndex(self.suggestions.index(0, 0))
        self._show_popup()

    def _show_popup(self) -> None:
        window = self.window()
        if self.popup.parentWidget() is not window:
            self.popup.setParent(window)
        rows = min(self.suggestions.rowCount(), 8)
        row_height = self.popup.sizeHintForRow(0) if rows else self.fontMetrics().height()
        width = max(self.width(), 320)
        self.popup.setGeometry(
            QRect(
                self.mapTo(window, QPoint(0, self.height())),
                QSize(width, rows * row_height + 2 * self.popup.frameWidth() + 2),
            )
        )
        self.popup.raise_()
        self.popup.show()

    def hide_popup(self) -> None:
        self.popup.hide()

    def _move(self, step: int) -> None:
        rows = self.suggestions.rowCount()
        if not rows or not self.suggestion_ids():
            return
        row = self.popup.currentIndex().row() if self.popup.currentIndex().isValid() else -1
        self.popup.setCurrentIndex(self.suggestions.index((row + step) % rows, 0))

    def _choose(self, index: QModelIndex, exclude: bool) -> None:
        tag_id = index.data(_TAG_ID) if index.isValid() else None
        if tag_id is None:
            return
        self.clear()
        self.suggestions.clear()
        self.popup.hide()
        self.picked.emit(int(tag_id), exclude)

    def _clicked(self, index: QModelIndex) -> None:
        shift = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        self._choose(index, shift)
        self.setFocus()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.popup.isVisible():
                shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self._choose(self.popup.currentIndex(), shift)
            event.accept()
            return
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self.popup.isVisible():
            self._move(1 if key == Qt.Key.Key_Down else -1)
            event.accept()
            return
        if key == Qt.Key.Key_Escape and self.popup.isVisible():
            self.popup.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        # A click in the list arrives after focus leaves; let it land before hiding.
        self._hide_timer.start()
        super().focusOutEvent(event)

    def _hide_unless_focused(self) -> None:
        if not self.hasFocus():
            self.popup.hide()


class FilterBar(QWidget):
    """Text box, tag box, and chips. Emits :attr:`changed` with the new :class:`Filters`."""

    changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tree: TagTree | None = None
        self._include: list[int] = []
        self._exclude: list[int] = []
        self._chips: dict[int, Chip] = {}
        self._only: ScopeChip | None = None
        self._applied_text = ""

        self.text_edit = QLineEdit()
        self.text_edit.setPlaceholderText("Search…")
        self.text_edit.setClearButtonEnabled(True)
        self.text_edit.setToolTip("Find items by name or text (Ctrl+F)")
        self._text_timer = QTimer(self)
        self._text_timer.setSingleShot(True)
        self._text_timer.setInterval(TEXT_DELAY_MS)
        self._text_timer.timeout.connect(self._apply_text)
        self.text_edit.textChanged.connect(lambda _: self._text_timer.start())
        self.text_edit.returnPressed.connect(self._apply_text)

        self.tag_edit = TagBox()
        self.tag_edit.picked.connect(lambda tag_id, exclude: self.add_tag(tag_id, exclude=exclude))

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.text_edit, 3)
        top.addWidget(self.tag_edit, 2)

        self.chip_area = QWidget()
        self.chip_layout = FlowLayout(self.chip_area)
        self.clear_button = QPushButton("Clear all")
        self.clear_button.setFlat(True)
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.setToolTip("Remove every tag filter and the search text")
        self.clear_button.clicked.connect(self.clear)
        self.chip_layout.addWidget(self.clear_button)
        self.chip_area.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.chip_area.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(top)
        layout.addWidget(self.chip_area)

        find = QShortcut(QKeySequence.StandardKey.Find, self)
        find.activated.connect(self.focus_text)

    # --- state ---

    def filters(self) -> Filters:
        only = self._only.type_id if self._only is not None else None
        return Filters(tuple(self._include), tuple(self._exclude), self._applied_text, only)

    def set_only(self, type_id: str | None, label: str = "") -> None:
        """Show an "Only: <label>" chip (first), or remove it with ``None``."""
        if self._only is not None:
            if self._only.type_id == type_id:
                return
            self.chip_layout.removeWidget(self._only)
            self._only.deleteLater()
            self._only = None
        if type_id is not None:
            self._only = ScopeChip(type_id, label)
            self._only.removed.connect(lambda: self.set_only(None))
            self.chip_layout.insert_widget(0, self._only)
        self._changed()

    def set_tree(self, tree: TagTree) -> None:
        """Use ``tree`` for chip names and suggestions (loaded in a worker by the page)."""
        self.tree = tree
        self.tag_edit.set_tree(tree)
        for chip in self._chips.values():
            chip.set_tree(tree)

    def add_tag(self, tag_id: int, *, exclude: bool = False) -> None:
        """Add an include chip, or a "but not" chip. A tag already in the other list moves."""
        target = self._exclude if exclude else self._include
        if tag_id in target:
            return
        if tag_id in self._chips:
            self._drop(tag_id)
        target.append(tag_id)
        chip = Chip(tag_id, exclude)
        chip.set_tree(self.tree)
        chip.removed.connect(self.remove_tag)
        self._chips[tag_id] = chip
        # The "Only" chip, include chips, exclude chips, then "Clear all".
        position = int(self._only is not None) + (
            len(self._include) - 1 if not exclude else len(self._include) + len(self._exclude) - 1
        )
        self.chip_layout.insert_widget(position, chip)
        self._changed()

    def remove_tag(self, tag_id: int) -> None:
        if tag_id in self._chips:
            self._drop(tag_id)
            self._changed()

    def clear(self) -> None:
        """Remove every chip and the text."""
        for tag_id in list(self._chips):
            self._drop(tag_id)
        if self._only is not None:
            self.chip_layout.removeWidget(self._only)
            self._only.deleteLater()
            self._only = None
        self.text_edit.clear()
        self._text_timer.stop()
        self._applied_text = ""
        self._changed()

    def focus_text(self) -> None:
        self.text_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.text_edit.selectAll()

    def _drop(self, tag_id: int) -> None:
        chip = self._chips.pop(tag_id)
        (self._exclude if chip.exclude else self._include).remove(tag_id)
        self.chip_layout.removeWidget(chip)
        chip.deleteLater()

    def _apply_text(self) -> None:
        self._text_timer.stop()
        text = self.text_edit.text().strip()
        if text != self._applied_text:
            self._applied_text = text
            self._changed()

    def _changed(self) -> None:
        self.chip_area.setVisible(bool(self._chips) or self._only is not None)
        self.chip_layout.invalidate()
        self.changed.emit(self.filters())
