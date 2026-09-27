"""QApplication setup."""

import logging
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

import tagalot
from tagalot.core.db import check_sqlite_support
from tagalot.core.session import KeepSession
from tagalot.core.settings import load_settings
from tagalot.ui.main_window import MainWindow
from tagalot.ui.opening import open_keep_async

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

    windows: list[MainWindow] = [MainWindow()]
    windows[0].show()
    logger.info("Main window shown")

    keep_args = [a for a in argv[1:] if not a.startswith("-")]
    if keep_args:
        # `tagalot <keep folder>` opens a keep directly (the launcher arrives in #52).
        def opened(session: KeepSession) -> None:
            window = MainWindow(session)
            window.show()
            windows[0].close()
            windows.append(window)

        open_keep_async(windows[0], Path(keep_args[0]), load_settings(), opened)

    code = app.exec()
    for window in windows:
        if window.session is not None:
            window.session.close()
    return code
