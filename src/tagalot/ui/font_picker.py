"""Choosing a keep's font (#565, DESIGN.md §12 *Fonts*): an installed family, with a preview
of the keep's own titles; a Google Font, downloaded into the keep when asked (#566); or a
font file.
"""

from collections.abc import Callable, Set
from dataclasses import dataclass
from pathlib import Path

import shiboken6
from PySide6.QtCore import (
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    QStringListModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QFont, QFontDatabase, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.fonts import FONT_EXTENSIONS
from tagalot.core.google_fonts import HOST, GoogleFont, Progress, google_fonts
from tagalot.ui.fonts import installed, place_font
from tagalot.ui.workers import run_in_pool

PLACE_LABELS = {"titles": "Titles", "notes": "Notes", "text": "Text"}
CATEGORY_LABELS = {
    "": "All kinds",
    "serif": "Serif",
    "sans-serif": "Sans serif",
    "display": "Display",
    "handwriting": "Handwriting",
    "monospace": "Monospace",
}
_HINTS = {
    "serif": QFont.StyleHint.Serif,
    "sans-serif": QFont.StyleHint.SansSerif,
    "display": QFont.StyleHint.Decorative,
    "handwriting": QFont.StyleHint.Cursive,
    "monospace": QFont.StyleHint.Monospace,
}
_FONT = Qt.ItemDataRole.UserRole + 1

Download = Callable[[GoogleFont, Progress], str]
"""Downloads a Google Font into the keep (in a worker) and returns its ``keep.toml`` value."""


@dataclass(frozen=True)
class FontChoice:
    """What the picker gives: an installed family, a font already in the keep folder (a
    downloaded Google Font's ``fonts/…``), or a font file to copy into the keep."""

    family: str | None = None
    file: Path | None = None
    value: str | None = None


class _GoogleFilter(QSortFilterProxyModel):
    """Google Fonts by name and category."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.text = ""
        self.category = ""

    def filterAcceptsRow(self, row: int, parent: QModelIndex | QPersistentModelIndex) -> bool:
        model = self.sourceModel()
        font = model.index(row, 0, parent).data(_FONT)
        if not isinstance(font, GoogleFont):
            return False
        if self.category and font.category != self.category:
            return False
        return self.text.casefold() in font.name.casefold()

    def refilter(self, text: str | None = None, category: str | None = None) -> None:
        self.beginFilterChange()
        if text is not None:
            self.text = text
        if category is not None:
            self.category = category
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)


class FontPicker(QDialog):
    """Pick a family from those installed (filtered by typing), seeing ``samples`` (the
    keep's own titles) in it; a Google Font (``download`` fetches it, ``here`` names those
    already in the keep); or a TTF or OTF file."""

    _progressed = Signal(int, int)

    def __init__(
        self,
        place: str,
        current: str | None,
        samples: list[str],
        parent: QWidget | None = None,
        *,
        here: Set[str] = frozenset(),
        download: Download | None = None,
    ) -> None:
        super().__init__(parent)
        self.place = place
        self.choice: FontChoice | None = None
        self.here = set(here)
        self.download = download
        self.downloading = False
        self.setWindowTitle(f"{PLACE_LABELS.get(place, place)} font")
        self.resize(600, 520)
        self.samples = samples or ["The quick brown fox jumps over the lazy dog"]
        self.preview = QLabel()
        self.preview.setObjectName("font_preview")
        self.preview.setWordWrap(True)
        self.preview.setMinimumHeight(110)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.preview_note = QLabel()
        self.preview_note.setStyleSheet("color: palette(placeholder-text);")
        self.tabs = QTabWidget()
        self.tabs.addTab(self._installed_tab(current), "Installed")
        self.tabs.addTab(self._google_tab(), "Google Fonts")
        self.tabs.currentChanged.connect(lambda _: self._show_preview())
        file_button = QPushButton("From a file…")
        file_button.setToolTip(
            "A TrueType (.ttf) or OpenType (.otf) file, copied into the keep folder so the "
            "keep looks the same on another computer"
        )
        file_button.clicked.connect(self._from_file)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(file_button)
        row.addStretch(1)
        row.addWidget(self.buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.preview)
        layout.addWidget(self.preview_note)
        layout.addLayout(row)
        self._progressed.connect(self._show_progress)
        self._show_preview()

    # --- installed fonts ---

    def _installed_tab(self, current: str | None) -> QWidget:
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
        if current is not None:
            self.select(current)
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(self.filter)
        layout.addWidget(self.list, 1)
        return tab

    def select(self, family: str) -> None:
        """Select ``family`` in the installed list, case aside."""
        for row in range(self.proxy.rowCount()):
            index = self.proxy.index(row, 0)
            if str(index.data()).casefold() == family.casefold():
                self.list.setCurrentIndex(index)
                self.list.scrollTo(index)
                return

    def family(self) -> str | None:
        index = self.list.currentIndex()
        return str(index.data()) if index.isValid() else None

    # --- Google Fonts (#566) ---

    def _google_tab(self) -> QWidget:
        self.google_filter = QLineEdit()
        self.google_filter.setObjectName("google_filter")
        self.google_filter.setPlaceholderText("Search Google Fonts")
        self.google_filter.setClearButtonEnabled(True)
        self.category = QComboBox()
        self.category.setObjectName("google_category")
        for key, label in CATEGORY_LABELS.items():
            self.category.addItem(label, key)
        self.google_model = QStandardItemModel(self)
        for font in google_fonts().families:
            item = QStandardItem(self._label(font))
            item.setData(font, _FONT)
            item.setEditable(False)
            self.google_model.appendRow(item)
        self.google_proxy = _GoogleFilter(self)
        self.google_proxy.setSourceModel(self.google_model)
        self.google_filter.textChanged.connect(lambda text: self.google_proxy.refilter(text=text))
        self.category.currentIndexChanged.connect(
            lambda _: self.google_proxy.refilter(category=self.category.currentData())
        )
        self.google_list = QListView()
        self.google_list.setObjectName("google_families")
        self.google_list.setModel(self.google_proxy)
        self.google_list.setUniformItemSizes(True)
        self.google_list.selectionModel().currentChanged.connect(self._google_chosen)
        self.google_list.doubleClicked.connect(lambda _: self.accept())
        self.download_button = QPushButton("Download")
        self.download_button.setObjectName("google_download")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.start_download)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.google_status = QLabel(
            f"Download fetches the font and its license from {HOST} (Google Fonts' "
            "repository on GitHub) into this keep's fonts folder, so it's there on any "
            "computer. Nothing is downloaded until you ask."
        )
        self.google_status.setObjectName("google_status")
        self.google_status.setWordWrap(True)
        self.google_status.setStyleSheet("color: palette(placeholder-text);")
        top = QHBoxLayout()
        top.addWidget(self.google_filter, 1)
        top.addWidget(self.category)
        bottom = QHBoxLayout()
        bottom.addWidget(self.download_button)
        bottom.addWidget(self.progress, 1)
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addLayout(top)
        layout.addWidget(self.google_list, 1)
        layout.addLayout(bottom)
        layout.addWidget(self.google_status)
        return tab

    def _label(self, font: GoogleFont) -> str:
        kind = CATEGORY_LABELS.get(font.category, font.category).lower()
        here = ", in this keep" if font.name in self.here else ""
        return f"{font.name}  ({kind}{here})"

    def google_font(self) -> GoogleFont | None:
        font = self.google_list.currentIndex().data(_FONT)
        return font if isinstance(font, GoogleFont) else None

    def select_google(self, name: str) -> None:
        """Select a Google Font by name (filtering to it)."""
        self.google_filter.setText(name)
        for row in range(self.google_proxy.rowCount()):
            index = self.google_proxy.index(row, 0)
            font = index.data(_FONT)
            if isinstance(font, GoogleFont) and font.name == name:
                self.google_list.setCurrentIndex(index)
                return

    def _google_chosen(self, *_: QModelIndex) -> None:
        font = self.google_font()
        self.download_button.setEnabled(font is not None and not self.downloading)
        self.download_button.setText(
            "Use" if font is not None and font.name in self.here else "Download"
        )
        self._show_preview()

    def start_download(self) -> None:
        """Download the chosen Google Font into the keep (or use it, when it's there), then
        give it as the choice."""
        font = self.google_font()
        if font is None or self.downloading or self.download is None:
            return
        self.downloading = True
        self.download_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.google_status.setText(f"Downloading {font.name} from {HOST}…")
        download, report = self.download, self._progressed.emit

        def done(value: str) -> None:
            if not shiboken6.isValid(self):
                return
            self.downloading = False
            self.choice = FontChoice(value=value)
            super(FontPicker, self).accept()

        def failed(error: BaseException) -> None:
            if not shiboken6.isValid(self):
                return
            self.downloading = False
            self.progress.setVisible(False)
            self.download_button.setEnabled(True)
            self.google_status.setText(
                f"{error} You can pick an installed font instead, or one From a file…"
            )

        run_in_pool(lambda: download(font, report), on_done=done, on_error=failed)

    def _show_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    # --- both ---

    def _show_preview(self, *_: QModelIndex) -> None:
        base = QFont(self.font())
        note = ""
        if self.tabs.currentIndex() == 1:
            font = self.google_font()
            if font is not None and installed(font.name):
                base.setFamilies([font.name])
            elif font is not None:  # a font of its kind on this computer
                hinted = QFont()
                hinted.setStyleHint(_HINTS.get(font.category, QFont.StyleHint.AnyStyle))
                base.setFamilies([hinted.defaultFamily()])
                note = "Shown in a similar font until it's downloaded."
        else:
            family = self.family()
            if family:
                base.setFamilies([family])
        preview = place_font(self.place, base=base, bold=self.place == "titles", larger=4)
        preview.setFamilies(base.families())  # what's chosen here, over the keep's own
        self.preview.setFont(preview)
        self.preview.setText("\n".join(self.samples[:3]))
        self.preview_note.setText(note)
        self.preview_note.setVisible(bool(note))

    def _from_file(self) -> None:
        pattern = " ".join(f"*{ext}" for ext in FONT_EXTENSIONS)
        path, _ = QFileDialog.getOpenFileName(self, "Font file", "", f"Fonts ({pattern})")
        if path:
            self.choice = FontChoice(file=Path(path))
            super().accept()

    def accept(self) -> None:
        if self.tabs.currentIndex() == 1:
            self.start_download()
            return
        family = self.family()
        if family is None:
            return
        self.choice = FontChoice(family=family)
        super().accept()

    def reject(self) -> None:
        if not self.downloading:  # a download finishes first
            super().reject()
