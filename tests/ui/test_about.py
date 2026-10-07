"""Help → About Tagalot (#359): the tower, the version and formats, the links, and copying
what ``tagalot --check`` reports."""

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication, QDialog, QLabel
from pytestqt.qtbot import QtBot

import tagalot
from tagalot.core.keep import KEEP_FORMAT_VERSION
from tagalot.resources import TOWER_SIZE, tower_file
from tagalot.themes.api import API_VERSION
from tagalot.ui import about
from tagalot.ui.about import DOCUMENTATION, AboutDialog
from tagalot.ui.main_window import MainWindow
from tests.ui.test_music_views import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


def test_the_tower_is_shipped() -> None:
    with Image.open(str(tower_file())) as image:
        assert image.size == (TOWER_SIZE, TOWER_SIZE)
        assert image.mode == "RGBA"


def test_what_it_says(qtbot: QtBot) -> None:
    dialog = AboutDialog()
    qtbot.addWidget(dialog)
    texts = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert f"Tagalot {tagalot.__version__}" in texts
    assert f"Theme API {API_VERSION} · Keep format {KEEP_FORMAT_VERSION}" in texts
    assert "MIT License" in texts
    assert f'href="{DOCUMENTATION}"' in dialog.links.text()
    assert dialog.links.openExternalLinks()
    tower = dialog.findChild(QLabel, "tower")
    assert tower is not None
    assert not tower.pixmap().isNull()


def test_copying_the_check_report(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(about, "run_check", lambda out: out("Tagalot 9.9") or 0)
    dialog = AboutDialog()
    qtbot.addWidget(dialog)
    dialog.copy_button.click()
    assert not dialog.copy_button.isEnabled()  # while it checks
    qtbot.waitUntil(lambda: dialog.status.text() == "Copied the check report.", timeout=5000)
    assert QApplication.clipboard().text() == "Tagalot 9.9"
    assert dialog.copy_button.isEnabled()


def test_help_menu_opens_it(
    qtbot: QtBot, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[str] = []

    def exec_(self: QDialog) -> int:
        shown.append(self.windowTitle())
        return 0

    monkeypatch.setattr(AboutDialog, "exec", exec_)
    window.about_action.trigger()
    assert shown == ["About Tagalot"]
