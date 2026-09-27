"""Opening a keep from the UI: in the background, asking before migrations (DESIGN.md §12).

Used by the command line (``tagalot <keep folder>``) and by the keep launcher.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import QMessageBox, QWidget

from tagalot.core.db import KeepNeedsMigration
from tagalot.core.keep import NETWORK_WARNING, KeepError
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings, save_settings
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)


def open_keep_async(
    parent: QWidget | None,
    keep_dir: Path,
    settings: Settings,
    on_opened: Callable[[KeepSession], object],
    *,
    settings_path: Path | None = None,
    allow_migration: bool = False,
    on_failed: Callable[[BaseException], object] | None = None,
) -> None:
    """Open ``keep_dir`` in a worker and call ``on_opened(session)`` on the GUI thread.

    A keep that needs upgrading asks first (a backup is made before migrating). Other
    problems are shown in a message box and passed to ``on_failed``. A successful open adds
    the keep to the recent keeps and warns if it is on a network drive.
    """

    def work() -> KeepSession:
        session = KeepSession.open(keep_dir, settings, allow_migration=allow_migration)
        settings.add_recent_keep(session.keep.dir)
        save_settings(settings, settings_path)
        return session

    def opened(session: KeepSession) -> None:
        if session.keep.on_network:
            QMessageBox.warning(parent, "Keep on a network drive", NETWORK_WARNING)
        on_opened(session)

    def failed(error: BaseException) -> None:
        if isinstance(error, KeepNeedsMigration):
            answer = QMessageBox.question(
                parent,
                "Upgrade this keep?",
                f"{error}\n\nUpgrade it now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                open_keep_async(
                    parent,
                    keep_dir,
                    settings,
                    on_opened,
                    settings_path=settings_path,
                    allow_migration=True,
                    on_failed=on_failed,
                )
                return
        elif isinstance(error, KeepError):
            QMessageBox.warning(parent, "Can't open this keep", str(error))
        else:
            logger.error("Opening %s failed", keep_dir, exc_info=error)
            QMessageBox.critical(parent, "Can't open this keep", f"Something went wrong: {error}")
        if on_failed is not None:
            on_failed(error)

    run_in_pool(work, on_done=opened, on_error=failed)
