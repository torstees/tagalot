"""The activity panel and the status bar's problem badge (#111)."""

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMenu
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.ui.main_window import MainWindow
from tests.ui.test_contents_search import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures: the assets demo


def test_the_panel_is_hidden_until_asked_for(qtbot: QtBot, window: MainWindow) -> None:
    assert not window.activity.isVisible()
    assert not window.problem_badge.isVisible()  # no problems yet
    view = next(a.menu() for a in window.menuBar().actions() if a.text() == "&View")
    assert isinstance(view, QMenu)
    labels = [a.text() for a in view.actions()]
    assert "Activity" in labels
    window.show_activity()
    assert window.activity.isVisible()
    qtbot.waitUntil(lambda: "Asset files" in window.activity.folders.text(), timeout=5000)
    assert window.activity.heading.text() == "<b>Problems</b>: none"


def test_a_scans_problems_show_on_the_badge_and_panel(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    nothing_is_launched: list[tuple[str, str]],
) -> None:
    broken = Path(session.root_path("assets")) / "Kenji Sato" / "broken.png"
    broken.write_bytes(b"not an image")
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    qtbot.waitUntil(window.problem_badge.isVisible, timeout=5000)
    assert window.problem_badge.text().startswith("\u26a0 ")
    assert window.activity.now.text().startswith("Last scan, ")

    window.problem_badge.click()
    assert window.activity.isVisible()
    rows = window.activity.problems.rows
    warning = next(r for r in rows if r.problem.kind == "warning")
    assert warning.where == "Asset files \u203a Kenji Sato/broken.png"
    assert warning.path == str(broken)

    window.activity.table.selectRow(rows.index(warning))
    window.activity.reveal_button.click()
    assert nothing_is_launched == [("reveal", str(broken))]

    window.activity.copy()
    assert "Kenji Sato/broken.png" in QApplication.clipboard().text()
    window.activity.clear_button.click()
    assert window.activity.problems.rows == []
    assert not window.problem_badge.isVisible()


def test_an_offline_folder(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    root = Path(session.root_path("assets"))
    os.replace(root, root.with_name("asset-files-away"))
    try:
        window.show_activity()
        with qtbot.waitSignal(window.scans.finished, timeout=10_000):
            window.scan_now()
        qtbot.waitUntil(lambda: "offline" in window.activity.folders.text(), timeout=5000)
        qtbot.waitUntil(window.problem_badge.isVisible, timeout=5000)
        assert window.activity.problems.rows[0].problem.kind == "offline"
    finally:
        os.replace(root.with_name("asset-files-away"), root)
