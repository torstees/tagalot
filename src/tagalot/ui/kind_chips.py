"""The chips above a list that hide and show kinds of items (DESIGN.md §12 *Kind chips*,
#513): a file type (``PDF 3``) or a kind of item (``Folders 4``) each, switched off and on
in place, with what's hidden remembered per view and tab by the window.

The kinds come from :func:`~tagalot.core.search.count_by_kind`; the search leaves out what's
hidden (``SearchSpec.hidden_kinds``).
"""

from collections.abc import Iterable, Mapping

import shiboken6
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import QToolButton, QWidget

from tagalot.core.kinds import NO_EXTENSION, is_file_kind
from tagalot.ui.filter_bar import FlowLayout


def kind_label(
    kind: str, plurals: Mapping[str, str], own_types: Iterable[str] = (), contents: bool = False
) -> str:
    """What a kind's chip says: a file type's extension (``PDF``), or a type's plural
    (``Folders``). The page's own types say **No file** (a book without one, on the Books
    page), unless the list shows contents too, where they're one kind among several."""
    if is_file_kind(kind):
        return "No extension" if kind == NO_EXTENSION else kind[1:].upper()
    if kind in own_types and not contents:
        return "No file"
    return plurals.get(kind, kind)


def kind_tip(kind: str, label: str, plurals: Mapping[str, str], shown: bool) -> str:
    """A chip's tooltip: what clicking it does."""
    verb = "Hide" if shown else "Show"
    if is_file_kind(kind):
        what = "files without an extension" if kind == NO_EXTENSION else f"{label} files"
    elif label == "No file":
        what = f"{plurals.get(kind, kind).lower()} without a file"
    else:
        what = label.lower()
    return f"{verb} {what} (Ctrl+click: only these, or all again)"


class KindChips(QWidget):
    """A row of toggle chips, one per kind in the list, shown when it holds two kinds or
    more (or a hidden one). A chip that's off stays where it is, dimmed and struck through.
    Click toggles a kind; Ctrl+click shows only that kind, and again shows them all.

    Emits :attr:`changed` with the hidden kinds after the user changes them.
    """

    changed = Signal(list)
    """The kinds now hidden (sorted), after a click."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("kind_chips")
        self._layout = FlowLayout(self, spacing=4)
        self._hidden: set[str] = set()
        self._counts: dict[str, int] = {}
        self._labels: dict[str, str] = {}
        self._tips: dict[str, tuple[str, str]] = {}
        self.chips: dict[str, QToolButton] = {}
        self.setVisible(False)

    @property
    def hidden(self) -> tuple[str, ...]:
        """The hidden kinds, those not in the list now too (they apply when they're back)."""
        return tuple(sorted(self._hidden))

    def set_hidden(self, kinds: Iterable[str]) -> None:
        """Hide ``kinds`` (a remembered choice), without emitting :attr:`changed`."""
        self._hidden = set(kinds)
        self._update()

    def set_kinds(
        self,
        counts: Mapping[str, int],
        labels: Mapping[str, str],
        tips: Mapping[str, tuple[str, str]],
    ) -> None:
        """The list's kinds, how many items have each, their labels, and their tooltips
        (when shown, when hidden)."""
        if dict(counts) == self._counts and dict(labels) == self._labels:
            return
        self._counts, self._labels, self._tips = dict(counts), dict(labels), dict(tips)
        for chip in self.chips.values():
            self._layout.removeWidget(chip)
            chip.deleteLater()
        self.chips = {}
        # Kinds of items first, then file types, each by label.
        order = sorted(counts, key=lambda k: (is_file_kind(k), labels.get(k, k).lower()))
        for kind in order:
            chip = QToolButton()
            chip.setObjectName(f"kind_chip_{kind}")
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _=False, k=kind: self._clicked(k))
            self._layout.addWidget(chip)
            self.chips[kind] = chip
        self._update()

    def toggle(self, kind: str, *, only: bool = False) -> None:
        """What a click on ``kind``'s chip does (``only``: a Ctrl+click), emitting
        :attr:`changed`."""
        present = set(self._counts)
        if only:
            if present - {kind} <= self._hidden and kind not in self._hidden:
                self._hidden.clear()  # only it already: all again
            else:
                self._hidden = (self._hidden - present) | (present - {kind})
        elif kind in self._hidden:
            self._hidden.discard(kind)
        else:
            self._hidden.add(kind)
        self._update()
        self.changed.emit(list(self.hidden))

    def _clicked(self, kind: str) -> None:
        ctrl = QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier
        self.toggle(kind, only=bool(ctrl))

    def _update(self) -> None:
        if not shiboken6.isValid(self):
            return
        dim = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText)
        for kind, chip in self.chips.items():
            shown = kind not in self._hidden
            chip.setChecked(shown)
            chip.setText(f"{self._labels.get(kind, kind)} {self._counts.get(kind, 0):,}")
            when_shown, when_hidden = self._tips.get(kind, ("", ""))
            chip.setToolTip(when_shown if shown else when_hidden)
            font = chip.font()
            font.setStrikeOut(not shown)
            chip.setFont(font)
            palette = self.palette()
            if not shown:
                palette.setColor(QPalette.ColorRole.ButtonText, dim)
            chip.setPalette(palette)
        present = set(self._counts)
        self.setVisible(len(present) >= 2 or bool(present & self._hidden))
