"""QApplication setup."""

import logging
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox

import tagalot
from tagalot.core.db import check_sqlite_support
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings, load_settings
from tagalot.ui.launcher import LauncherDialog
from tagalot.ui.main_window import MainWindow
from tagalot.ui.opening import open_keep_async
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

SHUTDOWN_WAIT_MS = 30_000
"""How long quitting waits for background jobs (a keep finishing its writes) to finish."""


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

    tagalot_app = TagalotApp(load_settings())
    keep_args = [a for a in argv[1:] if not a.startswith("-")]
    if keep_args:
        # `tagalot <keep folder>` opens a keep directly; the launcher appears if it can't.
        open_keep_async(
            None,
            Path(keep_args[0]),
            tagalot_app.settings,
            tagalot_app.show_keep,
            on_failed=lambda _: tagalot_app.show_launcher(),
        )
    else:
        tagalot_app.show_launcher()

    code = app.exec()
    tagalot_app.close_all()
    # Let background jobs (a keep closing, a search) finish before Python shuts down, so
    # none of them reports back into a half-destroyed application.
    if not QThreadPool.globalInstance().waitForDone(SHUTDOWN_WAIT_MS):
        logger.warning("Background jobs were still running when Tagalot closed")
    return code


class TagalotApp:
    """Owns the launcher and one main window per open keep."""

    def __init__(self, settings: Settings, settings_path: Path | None = None) -> None:
        self.settings = settings
        self.settings_path = settings_path
        self.windows: list[MainWindow] = []
        self.launcher: LauncherDialog | None = None

    def show_launcher(self) -> LauncherDialog:
        """Show the keep launcher (reusing it if it's already open)."""
        if self.launcher is None or not self.launcher.isVisible():
            self.launcher = LauncherDialog(self.settings, settings_path=self.settings_path)
            self.launcher.opened.connect(self.show_keep)
        self.launcher.show()
        self.launcher.raise_()
        self.launcher.activateWindow()
        logger.info("Launcher shown")
        return self.launcher

    def show_keep(self, session: KeepSession) -> MainWindow:
        """Show a main window for an open keep. Closing the window closes the keep."""
        window = MainWindow(session, on_open_other=self.show_launcher)
        window.closed.connect(lambda: self._window_closed(window))
        window.show()
        self.windows.append(window)
        logger.info("Main window shown for %s", session.keep.config.name)
        return window

    def _window_closed(self, window: MainWindow) -> None:
        """Release the window's keep, so it can be reopened, moved, or deleted. Closing waits
        for queued writes, so it runs in a worker."""
        if window in self.windows:
            self.windows.remove(window)
        session = window.session
        window.deleteLater()
        if session is not None:
            name = session.keep.config.name
            run_in_pool(session.close, on_done=lambda _: logger.info("Closed %s", name))

    def close_all(self) -> None:
        """Close every keep still open (when the application quits)."""
        for window in self.windows:
            if window.session is not None:
                window.session.close()
        self.windows.clear()
