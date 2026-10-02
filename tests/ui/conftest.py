"""Shared set-up for the GUI tests."""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from tagalot.core.handlers import Command, FileToOpen
from tagalot.ui import main_window
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.navigation import NavTarget

LAUNCHED: list[tuple[str, str]] = []
"""What GUI tests asked the OS to launch, instead of launching it: (how, path or program)."""


@pytest.fixture(autouse=True)
def nothing_is_launched(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str]]]:
    """Opening a file in a test must never start a real program on the machine."""

    def record(how: str) -> object:
        def launch(self: FileOpener, target: FileToOpen | Command) -> None:
            LAUNCHED.append(
                (how, target.path if isinstance(target, FileToOpen) else target.program)
            )

        return launch

    for how in ("open", "reveal", "open_with", "start"):
        monkeypatch.setattr(FileOpener, how, record(how))
    LAUNCHED.clear()
    yield LAUNCHED
    LAUNCHED.clear()


@pytest.fixture(autouse=True)
def windows_open_on_search_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most GUI tests start from Search all, as windows did before the dashboard became the
    opening page (#115); ``test_opening.py`` checks the real opening page."""
    monkeypatch.setattr(main_window, "OPENING_PAGE", NavTarget("search", label="Search all"))


@pytest.fixture(autouse=True)
def no_modifiers_left_held() -> Iterator[None]:
    """Forget any keyboard modifier a test's key presses left "held".

    ``QTest.keyClick(widget, key, Shift)`` leaves Qt believing Shift is still down, and
    that changes how later tests' selections behave (``selectRow`` then extends or toggles).
    A plain key release afterwards resets Qt's idea of the modifiers.
    """
    yield
    if QApplication.instance() is None:
        return
    widget = QWidget()
    QTest.keyRelease(widget, Qt.Key.Key_Shift, Qt.KeyboardModifier.NoModifier)
    widget.deleteLater()
