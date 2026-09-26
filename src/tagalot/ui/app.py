"""QApplication setup."""

import logging
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication

import tagalot
from tagalot.ui.main_window import MainWindow

logger = logging.getLogger(__name__)


def run(argv: Sequence[str]) -> int:
    """Create the application and main window, run the event loop, and return its exit code."""
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(list(argv))
    app.setApplicationName("Tagalot")
    app.setApplicationVersion(tagalot.__version__)

    window = MainWindow()
    window.show()
    logger.info("Main window shown")
    return app.exec()
