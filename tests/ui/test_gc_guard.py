"""Garbage is collected on the GUI thread, never in a worker (#406)."""

import gc

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from tagalot.ui.gc_guard import GuiThreadCollector, collect_on_gui_thread

pytestmark = pytest.mark.gui


class _Cycle:
    def __init__(self) -> None:
        self.me = self


def test_automatic_collection_is_off_while_the_app_runs(qapp: QApplication) -> None:
    collector = collect_on_gui_thread(qapp)
    assert not gc.isenabled()
    assert collect_on_gui_thread(qapp) is collector  # one per application


def test_a_full_generation_is_collected_when_checked(qapp: QApplication) -> None:
    collector = collect_on_gui_thread(qapp)
    gc.collect()
    collector.check()
    assert collector.check() == 0  # nothing over its threshold yet
    for _ in range(collector.thresholds[0] + 1):
        _Cycle()
    assert collector.check() >= collector.thresholds[0]


def test_the_timer_collects_on_its_own(qtbot: QtBot, qapp: QApplication) -> None:
    collector = GuiThreadCollector(interval_ms=10)
    try:
        gc.collect()
        for _ in range(collector.thresholds[0] + 1):
            _Cycle()
        qtbot.waitUntil(lambda: gc.get_count()[0] < collector.thresholds[0], timeout=5000)
    finally:
        collector.deleteLater()
