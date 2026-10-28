"""Choosing a keep's font (#565, DESIGN.md §12 *Fonts*): an installed family, with a preview
of the keep's own titles, or a font file.
"""

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, QStringListModel, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
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

from tagalot.core.fonts import FONT_EXTENSIONS
from tagalot.ui.fonts import place_font

PLACE_LABELS = {"titles": "Titles", "notes": "Notes", "text": "Text"}


@dataclass(frozen=True)
class FontChoice:
    """What the picker gives: an installed family, or a font file to copy into the keep."""

    family: str | None = None
    file: Path | None = None


class FontPicker(QDialog):
    """Pick a family from those installed (filtered by typing), seeing ``samples`` (the
    keep's own titles) in it; or a TTF or OTF file."""

    def __init__(
        self,
        place: str,
        current: str | None,
        samples: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.place = place
        self.choice: FontChoice | None = None
        self.setWindowTitle(f"{PLACE_LABELS.get(place, place)} font")
        self.resize(560, 460)
        self.filter = QLineEdit()
        self.filter.setObjectName("font_filter")
        self.filter.setPlaceholderText("Filter fonts")
        self.filter.setClearButtonEnabled(True)
        families = sorted(set(QFontDatabase.families()), key=str.casefold)
        self.model = QStringListModel(families, self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.filter.textChanged.connect(self.proxy.setFilterFixedString)
        self.list = QListView()
        self.list.setObjectName("font_families")
        self.list.setModel(self.proxy)
        self.list.setUniformItemSizes(True)
        self.list.selectionModel().currentChanged.connect(self._show_preview)
        self.list.doubleClicked.connect(lambda _: self.accept())
        self.preview = QLabel()
        self.preview.setObjectName("font_preview")
        self.preview.setWordWrap(True)
        self.preview.setMinimumHeight(110)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.samples = samples or ["The quick brown fox jumps over the lazy dog"]
        file_button = QPushButton("From a file…")
        file_button.setToolTip(
            "A TrueType (.ttf) or OpenType (.otf) file, copied into the keep folder so the "
            "keep looks the same on another computer"
        )
        file_button.clicked.connect(self._from_file)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(file_button)
        row.addStretch(1)
        row.addWidget(buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(self.filter)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.preview)
        layout.addLayout(row)
        if current is not None:
            self.select(current)
        self._show_preview()

    def select(self, family: str) -> None:
        """Select ``family`` in the list, case aside."""
        for row in range(self.proxy.rowCount()):
            index = self.proxy.index(row, 0)
            if str(index.data()).casefold() == family.casefold():
                self.list.setCurrentIndex(index)
                self.list.scrollTo(index)
                return

    def family(self) -> str | None:
        index = self.list.currentIndex()
        return str(index.data()) if index.isValid() else None

    def _show_preview(self, *_: QModelIndex) -> None:
        family = self.family()
        base = QFont(self.font())
        if family:
            base.setFamilies([family])
        font = place_font(self.place, base=base, bold=self.place == "titles", larger=4)
        if family:
            font.setFamilies([family])  # over the keep's own, for the preview
        self.preview.setFont(font)
        self.preview.setText("\n".join(self.samples[:3]))

    def _from_file(self) -> None:
        pattern = " ".join(f"*{ext}" for ext in FONT_EXTENSIONS)
        path, _ = QFileDialog.getOpenFileName(self, "Font file", "", f"Fonts ({pattern})")
        if path:
            self.choice = FontChoice(file=Path(path))
            super().accept()

    def accept(self) -> None:
        family = self.family()
        if family is None:
            return
        self.choice = FontChoice(family=family)
        super().accept()
