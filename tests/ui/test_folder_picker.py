"""Folder pickers start where a folder was last chosen, and remember the choice."""

from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QFileDialog, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from tagalot.core.settings import Settings, load_settings
from tagalot.themes.loader import ThemeCatalog, load_themes
from tagalot.ui.folder_picker import choose_folder, start_folder
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
