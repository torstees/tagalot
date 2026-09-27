"""Tests for the main window and application startup."""

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox
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


def test_run_explains_unsupported_sqlite_and_exits(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(app, "check_sqlite_support", lambda: "Tagalot needs SQLite 3.45.")
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, text: shown.append((title, text)),
    )

    assert app.run(["tagalot"]) == 1
    assert shown == [("Tagalot cannot start", "Tagalot needs SQLite 3.45.")]
    assert not [w for w in qapp.topLevelWidgets() if isinstance(w, MainWindow) and w.isVisible()]
