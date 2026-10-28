"""Choosing an icon (#557, DESIGN.md §12 *Icons*): a searchable grid of the bundled Lucide
icons, a picture from a file, or the standard icon.

The dialog only says what was chosen (:class:`IconChoice`); the caller saves it, copying a
picture into the keep folder in a worker (``core/icons.py``).
"""

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.icons import PICTURE_EXTENSIONS, matching_icons
from tagalot.ui.icons import lucide

ICON_SIDE = 24
CELL = QSize(124, 76)
_NAME = Qt.ItemDataRole.UserRole + 1


@dataclass(frozen=True)
class IconChoice:
    """What was chosen: a Lucide icon's ``name``, a ``picture`` to copy into the keep, or
    neither (the standard icon: the theme's, or the generic one)."""

    name: str | None = None
    picture: Path | None = None


class IconPicker(QDialog):
    """Pick an icon. ``current`` is the icon chosen now (a Lucide name is selected);
    ``standard`` labels the button going back to the standard icon ("Use the theme's
    icon"), or None to offer none. After ``exec``, :attr:`choice` says what was picked."""

    def __init__(
        self,
        title: str,
        current: str | None = None,
        standard: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.choice: IconChoice | None = None
        self.search = QLineEdit()
        self.search.setObjectName("icon_search")
        self.search.setPlaceholderText("Search icons (film, person, book…)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.model = QStandardItemModel(self)
        self.grid = QListView()
        self.grid.setObjectName("icon_grid")
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setGridSize(CELL)
        self.grid.setIconSize(QSize(ICON_SIDE, ICON_SIDE))
        self.grid.setWordWrap(True)
        self.grid.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.setModel(self.model)
        self.grid.doubleClicked.connect(lambda _index: self.accept())
        self.none_found = QLabel("No icon matches.")
        self.none_found.setVisible(False)
        self.from_file = QPushButton("From a file…")
        self.from_file.setToolTip(
            "An SVG or PNG picture; it is copied into the keep folder, so the keep shows it "
            "on any computer."
        )
        self.from_file.clicked.connect(self._choose_file)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.standard: QPushButton | None = None
        if standard is not None:
            self.standard = QPushButton(standard)
            self.standard.clicked.connect(self.use_standard)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.grid.selectionModel().selectionChanged.connect(lambda *_: self._update_ok())
        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(self.grid, 1)
        layout.addWidget(self.none_found)
        row = QHBoxLayout()  # other choices on the left, OK and Cancel on the right
        if self.standard is not None:
            row.addWidget(self.standard)
        row.addWidget(self.from_file)
        row.addStretch(1)
        row.addWidget(buttons)
        layout.addLayout(row)
        self.resize(640, 480)
        self._filter("")
        self._select(current)
        self.search.setFocus()

    def names(self) -> list[str]:
        """The icons shown, in order."""
        return [self.model.item(row).data(_NAME) for row in range(self.model.rowCount())]

    def selected_name(self) -> str | None:
        """The Lucide icon selected in the grid, if any."""
        index = self.grid.currentIndex()
        if not index.isValid() or not self.grid.selectionModel().isSelected(index):
            return None
        return str(index.data(_NAME))

    def accept(self) -> None:
        if self.choice is None:
            name = self.selected_name()
            if name is None:
                return  # nothing picked yet
            self.choice = IconChoice(name)
        super().accept()

    def use_standard(self) -> None:
        self.choice = IconChoice()
        super().accept()

    def choose_picture(self, path: Path) -> None:
        """Take a picture from a file (copied into the keep by the caller)."""
        self.choice = IconChoice(picture=path)
        super().accept()

    def ask_picture(self) -> Path | None:
        """Ask for a picture file (tests replace this)."""
        patterns = " ".join(f"*{ext}" for ext in PICTURE_EXTENSIONS)
        path, _ = QFileDialog.getOpenFileName(
            self, "Icon from a file", "", f"Pictures ({patterns})"
        )
        return Path(path) if path else None

    def _choose_file(self) -> None:
        path = self.ask_picture()
        if path is not None:
            self.choose_picture(path)

    def _filter(self, text: str) -> None:
        chosen = self.selected_name()
        self.model.clear()
        for name in matching_icons(text):
            item = QStandardItem(lucide(name), name.replace("-", " "))  # wraps between words
            item.setData(name, _NAME)
            item.setToolTip(name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.model.appendRow(item)
        self.none_found.setVisible(self.model.rowCount() == 0)
        self._select(chosen)
        if self.selected_name() is None and self.model.rowCount() and text.strip():
            self.grid.setCurrentIndex(self.model.index(0, 0))  # Enter takes the first match
        self._update_ok()

    def _select(self, name: str | None) -> None:
        for row in range(self.model.rowCount()):
            if self.model.item(row).data(_NAME) == name:
                index = self.model.index(row, 0)
                self.grid.setCurrentIndex(index)
                self.grid.scrollTo(index)
                break
        self._update_ok()

    def _update_ok(self) -> None:
        self.ok.setEnabled(self.selected_name() is not None)
