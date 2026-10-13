"""Choosing a folder, starting where the last one was chosen (DESIGN.md §4 per-user settings).

Every folder picker in Tagalot goes through :func:`choose_folder`, so they all start in
the same place: the folder last chosen in any of them (``settings.last_folder``), which is
remembered in ``settings.toml`` across sessions.
"""

from pathlib import Path

import shiboken6
from PySide6.QtCore import QDir
from PySide6.QtWidgets import QDialog, QFileDialog, QWidget

from tagalot.core.settings import Settings, save_settings
from tagalot.ui.workers import run_in_pool


def start_folder(settings: Settings | None) -> str:
    """Where a folder picker (or a new keep's location) starts: the last folder chosen, if
    there was one, else the home folder."""
    if settings is not None and settings.last_folder is not None:
        return str(settings.last_folder)
    return str(Path.home())


def save_folder(settings: Settings | None) -> str:
    """Where saving an export (or opening a tag file) starts: the folder a file was last
    saved to or opened from, else where the folder pickers start."""
    if settings is not None and settings.last_save_folder is not None:
        return str(settings.last_save_folder)
    return start_folder(settings)


def remember_save_folder(
    settings: Settings | None, path: str, settings_path: Path | None = None
) -> None:
    """Remember ``path``'s folder for the next save or import, and save the settings in the
    background. A folder picker's choice (``last_folder``) is left alone."""
    if settings is None or not path:
        return
    settings.last_save_folder = Path(path).parent
    run_in_pool(lambda: save_settings(settings, settings_path))


def save_file_dialog(parent: QWidget | None, title: str, name: str, start: str) -> QFileDialog:
    """A save dialog for ``name`` in ``start``. A name with an extension filters by it and
    adds it to a name typed without one."""
    dialog = QFileDialog(parent, title)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dialog.setDirectory(start)
    dialog.selectFile(name)
    filters, suffix = save_filters(name)
    if suffix:
        dialog.setNameFilters(filters)
        dialog.setDefaultSuffix(suffix)
    return dialog


def save_filters(name: str) -> tuple[list[str], str]:
    """The save dialog's file filters and default extension for ``name``: a name with an
    extension filters by it, and a name typed without one gets it."""
    suffix = Path(name).suffix.lstrip(".")
    if not suffix:
        return [], ""
    return [f"{suffix.upper()} files (*.{suffix})", "All files (*)"], suffix


def choose_save_file(
    parent: QWidget | None,
    title: str,
    name: str,
    settings: Settings | None,
    settings_path: Path | None = None,
) -> str:
    """Ask where to save a file named ``name`` at first, starting where a file was last
    saved; the folder chosen is remembered for the next save (:func:`remember_save_folder`).
    Returns the path, or ``""`` if cancelled."""
    dialog = save_file_dialog(parent, title, name, save_folder(settings))
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selectedFiles():
            return ""
        path = dialog.selectedFiles()[0]
    finally:
        shiboken6.delete(dialog)  # now, with its folder model and the folder's watcher
    remember_save_folder(settings, path, settings_path)
    return path


def choose_open_file(
    parent: QWidget | None,
    title: str,
    filter_: str,
    settings: Settings | None,
    settings_path: Path | None = None,
) -> str:
    """Ask for a file to open, starting where a file was last saved or opened (the same
    place as :func:`choose_save_file`, so an export is easy to find again)."""
    path = QFileDialog.getOpenFileName(parent, title, save_folder(settings), filter_)[0]
    remember_save_folder(settings, path, settings_path)
    return path


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
    chosen_text = QFileDialog.getExistingDirectory(parent, title, start or start_folder(settings))
    folder = QDir.toNativeSeparators(chosen_text)  # Qt gives E:/Books on Windows too (#432)
    if folder and settings is not None:
        chosen = Path(folder)
        settings.last_folder = chosen.parent if remember_parent else chosen
        run_in_pool(lambda: save_settings(settings, settings_path))
    return folder
