"""The GUI tests' guard against modals nothing answers (#272): offscreen, one would wait
forever, so it fails at once instead, naming what opened."""

import pytest
from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog, QMenu, QMessageBox, QWidget
from pytestqt.qtbot import QtBot

from tests.ui.conftest import UnexpectedModal, _MenuCatcher

pytestmark = pytest.mark.gui


def test_a_message_box_fails_naming_its_text() -> None:
    with pytest.raises(
        UnexpectedModal, match=r"QMessageBox\.question \(Delete\?; Delete 3 tags\?\)"
    ):
        QMessageBox.question(None, "Delete?", "Delete 3 tags?")


def test_a_menu_is_closed_and_named(qtbot: QtBot, no_unexpected_modals: _MenuCatcher) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    menu = QMenu(parent)
    menu.addAction("Open page")
    menu.addAction("Reveal file")
    assert menu.exec(parent.mapToGlobal(parent.rect().center())) is None  # returns, closed
    assert no_unexpected_modals.caught == ["QMenu ''; Open page; Reveal file"]
    no_unexpected_modals.caught.clear()  # else this test would fail as it ends, as it should


def test_a_dialog_fails_naming_its_title(qtbot: QtBot) -> None:
    dialog = QDialog()
    qtbot.addWidget(dialog)
    dialog.setWindowTitle("Merge items")
    with pytest.raises(UnexpectedModal, match=r"QDialog\.exec \(QDialog 'Merge items'\)"):
        dialog.exec()


def test_input_and_file_dialogs_fail() -> None:
    with pytest.raises(UnexpectedModal, match=r"QInputDialog\.getText"):
        QInputDialog.getText(None, "New tag", "Name:")
    with pytest.raises(UnexpectedModal, match=r"QFileDialog\.getExistingDirectory"):
        QFileDialog.getExistingDirectory(None, "Add a folder")


def test_a_test_can_still_answer_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    assert QMessageBox.question(None, "Delete?", "Sure?") == QMessageBox.StandardButton.Yes
