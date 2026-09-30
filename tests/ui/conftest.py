"""Shared set-up for the GUI tests."""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget


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
