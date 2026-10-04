"""Choosing a folder, starting where the last one was chosen (DESIGN.md §4 per-user settings).

Every folder picker in Tagalot goes through :func:`choose_folder`, so they all start in
the same place: the folder last chosen in any of them (``settings.last_folder``), which is
remembered in ``settings.toml`` across sessions.
"""

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QWidget

from tagalot.core.settings import Settings, save_settings
from tagalot.ui.workers import run_in_pool


def start_folder(settings: Settings | None) -> str:
    """Where a folder picker (or a new keep's location) starts: the last folder chosen, if
    there was one, else the home folder."""
    if settings is not None and settings.last_folder is not None:
        return str(settings.last_folder)
    return str(Path.home())


def choose_folder(
    parent: QWidget | None,
    title: str,
    settings: Settings | None,
    settings_path: Path | None = None,
    *,
    start: str = "",
    remember_parent: bool = False,
) -> str:
    """Ask for a folder, starting at ``start`` or else the last folder chosen. Returns it,
    or ``""`` if cancelled. The choice is remembered (its parent, with ``remember_parent``:
    a keep folder is chosen to open it, but the folder holding keeps is where to look next)
    and saved in the background."""
    folder = QFileDialog.getExistingDirectory(parent, title, start or start_folder(settings))
    if folder and settings is not None:
        chosen = Path(folder)
        settings.last_folder = chosen.parent if remember_parent else chosen
        run_in_pool(lambda: save_settings(settings, settings_path))
    return folder
