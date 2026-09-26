"""Tests that the test harness itself is configured as AGENTS.md requires."""

import pytest
from PySide6.QtWidgets import QApplication


@pytest.mark.gui
def test_gui_tests_run_headless(qapp: QApplication) -> None:
    assert qapp.platformName() == "offscreen"


def test_markers_registered(pytestconfig: pytest.Config) -> None:
    registered = {line.split(":", 1)[0] for line in pytestconfig.getini("markers")}
    assert {"gui", "slow"} <= registered
