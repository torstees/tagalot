"""QApplication setup."""

import logging
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication, QMessageBox

import tagalot
from tagalot.core.db import check_sqlite_support
from tagalot.ui.main_window import MainWindow

logger = logging.getLogger(__name__)


def run(argv: Sequence[str]) -> int:
    """Create the application and main window, run the event loop, and return its exit code."""
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(list(argv))
    app.setApplicationName("Tagalot")
    app.setApplicationVersion(tagalot.__version__)

    if problem := check_sqlite_support():
        logger.error("Cannot start: %s", problem)
        QMessageBox.critical(None, "Tagalot cannot start", problem)
        return 1

    window = MainWindow()
    window.show()
    logger.info("Main window shown")
    return app.exec()
