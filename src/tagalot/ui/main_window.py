"""Main window: navigation, center stack, tagging panel, status bar."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QWidget

WINDOW_TITLE = "Tagalot"


class MainWindow(QMainWindow):
    """The top-level window. For now it only shows that no keep is open."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1200, 800)

        placeholder = QLabel("No keep open")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCentralWidget(placeholder)

        self.statusBar().showMessage("Ready")
