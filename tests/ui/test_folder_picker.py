"""Folder pickers start where a folder was last chosen, and remember the choice."""

import os
from pathlib import Path
from typing import Self

import pytest
import shiboken6
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QDialog, QFileDialog, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from tagalot.core.settings import Settings, load_settings
from tagalot.themes.loader import ThemeCatalog, load_themes
from tagalot.ui import folder_picker
from tagalot.ui.folder_picker import (
    choose_folder,
    choose_open_file,
    choose_save_file,
    save_filters,
    start_folder,
)
from tagalot.ui.launcher import NewKeepDialog

pytestmark = pytest.mark.gui


class Picker:
    """Stands in for the folder dialog: records where it started, answers from a list."""

    def __init__(self) -> None:
        self.started_at: list[str] = []
        self.answers: list[str] = []

    def __call__(self, parent: QWidget | None, title: str, start: str = "") -> str:
        self.started_at.append(start)
        return self.answers.pop(0)


@pytest.fixture
def picker(monkeypatch: pytest.MonkeyPatch) -> Picker:
    fake = Picker()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", fake)
    return fake


@pytest.fixture
def catalog(tmp_path: Path) -> ThemeCatalog:
    return load_themes(user_dir=tmp_path / "no-user-themes")


def _browse_buttons(dialog: NewKeepDialog) -> tuple[QPushButton, QPushButton]:
    location, root = [b for b in dialog.findChildren(QPushButton) if b.text() == "Browse…"]
    return location, root


def _saved(path: Path) -> Path | None:
    QThreadPool.globalInstance().waitForDone(5000)  # saving runs in the background
    return load_settings(path).last_folder


def test_a_new_keep_starts_where_a_folder_was_last_chosen(
    qtbot: QtBot, tmp_path: Path, catalog: ThemeCatalog, picker: Picker
) -> None:
    keeps, music = tmp_path / "Keeps", tmp_path / "Music"
    settings, path = Settings(last_folder=keeps), tmp_path / "settings.toml"
    dialog = NewKeepDialog(catalog, settings=settings, settings_path=path)
    qtbot.addWidget(dialog)
    assert dialog.location_edit.text() == str(keeps)  # not the home folder
    assert dialog.root_edit.text() == ""

    _, root = _browse_buttons(dialog)
    picker.answers = [str(music)]
    root.click()
    assert picker.started_at == [str(keeps)]  # the empty field starts at the last folder
    assert dialog.root_edit.text() == str(music)
    assert settings.last_folder == music
    assert _saved(path) == music


def test_choosing_the_location_moves_where_the_watched_folder_starts(
    qtbot: QtBot, tmp_path: Path, catalog: ThemeCatalog, picker: Picker
) -> None:
    settings = Settings()
    dialog = NewKeepDialog(catalog, settings=settings, settings_path=tmp_path / "s.toml")
    qtbot.addWidget(dialog)
    assert dialog.location_edit.text() == str(Path.home())  # nothing chosen yet

    location, root = _browse_buttons(dialog)
    picker.answers = [str(tmp_path / "D"), str(tmp_path / "D" / "Photos")]
    location.click()
    root.click()
    assert picker.started_at == [str(Path.home()), str(tmp_path / "D")]
    assert dialog.location_edit.text() == str(tmp_path / "D")
    assert dialog.root_edit.text() == str(tmp_path / "D" / "Photos")


def test_cancelling_changes_nothing(tmp_path: Path, picker: Picker) -> None:
    settings = Settings(last_folder=tmp_path)
    picker.answers = [""]
    assert choose_folder(None, "Pick", settings, tmp_path / "s.toml") == ""
    assert settings.last_folder == tmp_path
    assert not (tmp_path / "s.toml").exists()


def test_opening_a_keep_remembers_the_folder_holding_it(tmp_path: Path, picker: Picker) -> None:
    settings, path = Settings(), tmp_path / "s.toml"
    picker.answers = [str(tmp_path / "Keeps" / "Music.keep")]
    choose_folder(None, "Open a keep folder", settings, path, remember_parent=True)
    assert settings.last_folder == tmp_path / "Keeps"
    assert _saved(path) == tmp_path / "Keeps"


def test_start_folder() -> None:
    assert start_folder(None) == str(Path.home())
    assert start_folder(Settings()) == str(Path.home())
    assert start_folder(Settings(last_folder=Path("D:/Keeps"))) == str(Path("D:/Keeps"))


def test_a_save_dialog_adds_the_extension() -> None:
    # No real dialog is made here: its folder model slows the tests that come after it.
    assert save_filters("Demo tags.yaml") == (["YAML files (*.yaml)", "All files (*)"], "yaml")
    assert save_filters("notes") == ([], "")


class FakeSaveDialog:
    """Stands in for the save dialog: records where it started, answers with ``answer``."""

    def __init__(self, started: list[str], answer: str) -> None:
        self.started, self.answer = started, answer

    def __call__(self, parent: QWidget | None, title: str, name: str, start: str) -> "Self":
        self.started.append(start)
        return self

    def exec(self) -> int:
        return QDialog.DialogCode.Accepted.value

    def selectedFiles(self) -> list[str]:
        return [self.answer]


def test_saving_and_opening_start_where_a_file_was_last_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keeps, exports = tmp_path / "Keeps", tmp_path / "Exports"
    settings, path = Settings(last_folder=keeps), tmp_path / "s.toml"
    started: list[str] = []
    saved = str(exports / "Demo tags.yaml")
    monkeypatch.setattr(folder_picker, "save_file_dialog", FakeSaveDialog(started, saved))
    monkeypatch.setattr(shiboken6, "delete", lambda dialog: None)
    assert choose_save_file(None, "Save", "Demo tags.yaml", settings, path) == saved
    assert started == [str(keeps)]  # nothing saved yet: where the folder pickers start
    assert settings.last_save_folder == exports
    assert settings.last_folder == keeps  # left alone
    QThreadPool.globalInstance().waitForDone(5000)
    assert load_settings(path).last_save_folder == exports

    opened: list[str] = []

    def open_file(parent: QWidget | None, title: str, start: str, filter_: str) -> tuple[str, str]:
        opened.append(start)
        return str(tmp_path / "Other" / "more tags.yaml"), filter_

    monkeypatch.setattr(QFileDialog, "getOpenFileName", open_file)
    choose_open_file(None, "Import tags", "YAML files (*.yaml)", settings, path)
    assert opened == [str(exports)]
    assert settings.last_save_folder == tmp_path / "Other"


def test_a_chosen_folder_has_this_systems_separators(
    qtbot: QtBot, tmp_path: Path, picker: Picker
) -> None:
    r"""Qt's dialog answers ``E:/Books`` on Windows too; keep.toml gets ``E:\Books`` (#432)."""
    picker.answers = ["E:/Books/Fantasy"]
    chosen = choose_folder(None, "Watch a folder", Settings(), tmp_path / "settings.toml")
    assert chosen == "E:/Books/Fantasy".replace("/", os.sep)
    QThreadPool.globalInstance().waitForDone(5000)
