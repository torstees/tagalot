"""Tests for the main window and application startup."""

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel
from pytestqt.qtbot import QtBot

from tagalot.ui import app
from tagalot.ui.main_window import WINDOW_TITLE, MainWindow

pytestmark = pytest.mark.gui


def test_main_window_starts_empty(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()

    assert window.windowTitle() == WINDOW_TITLE == "Tagalot"
    central = window.centralWidget()
    assert isinstance(central, QLabel)
    assert central.text() == "No keep open"
    assert window.statusBar().currentMessage() == "Ready"


def test_run_shows_window_and_returns_exit_code(qapp: QApplication) -> None:
    shown: list[str] = []

    def inspect_then_quit() -> None:
        shown.extend(w.windowTitle() for w in qapp.topLevelWidgets() if w.isVisible())
        qapp.exit(0)

    QTimer.singleShot(0, inspect_then_quit)
    assert app.run(["tagalot"]) == 0
    assert "Tagalot" in shown
    assert qapp.applicationName() == "Tagalot"
