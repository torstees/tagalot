"""Tests for the main window with an open keep, and for opening keeps from the UI."""

import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot
from sqlalchemy import insert

from tagalot.core.keep import KeepError, RootConfig, ThemeRef, create_keep
from tagalot.core.models import SavedSearch
from tagalot.core.scanjob import ScanReport
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings, load_settings
from tagalot.core.ui_state import load_ui_state
from tagalot.ui.dedupe_view import DedupePage
from tagalot.ui.main_window import MainWindow, scan_summary
from tagalot.ui.navigation import NavTarget
from tagalot.ui.opening import open_keep_async
from tagalot.ui.search_view import SearchPage
from tagalot.ui.triage import TriagePage
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui


@pytest.fixture
def keep_dir(tmp_path: Path) -> Path:
    files = tmp_path / "files"
    files.mkdir()
    for name in ["beach.jpg", "family.jpg", "notes.txt"]:
        (files / name).write_bytes(b"x")
    roots = [RootConfig("r", "Photos", str(files))]
    return create_keep(tmp_path / "Photos.keep", "Photos", ThemeRef("generic", 1), roots).dir


@pytest.fixture
def session(keep_dir: Path) -> Iterator[KeepSession]:
    with KeepSession.open(keep_dir, Settings()) as session:
        yield session


def _window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.show()
    return window


def test_layout(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    assert window.windowTitle() == "Photos — Tagalot"
    assert window.scan_action.shortcut().toString() == "F5"
    assert [t.label for t in window.navigation.targets("searches")] == [
        "Files",
        "Recently modified",
    ]
    assert [t.label for t in window.navigation.targets("tools")] == [
        "Triage",
        "Dedupe",
        "File keywords",
        "Tag manager",
    ]
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)  # "Search all" is the default page
    assert page.grouped
    assert page.current_spec() == SearchSpec()
    assert window.tags_dock.isVisible()


def test_saved_searches_load_in_the_background(qtbot: QtBot, session: KeepSession) -> None:
    session.writer.run(
        lambda conn: conn.execute(
            insert(SavedSearch), [{"name": n, "definition": {}} for n in ["Iceland", "Beach"]]
        )
    )
    window = _window(qtbot, session)
    qtbot.waitUntil(lambda: len(window.navigation.targets("saved")) == 2, timeout=5000)
    assert [t.label for t in window.navigation.targets("saved")] == ["Beach", "Iceland"]


def test_navigating_switches_pages(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    window.navigation.select(NavTarget("dedupe", label="Dedupe"))
    assert isinstance(window.stack.currentWidget(), DedupePage)
    window.navigation.select(NavTarget("triage", label="Triage"))
    assert isinstance(window.stack.currentWidget(), TriagePage)


def test_folded_sections_are_remembered_per_keep(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    heading = window.navigation.model().index(3, 0)  # TOOLS
    window.navigation.clicked.emit(heading)
    assert window.navigation.folded() == {"tools"}
    assert heading.data().startswith("▸ TOOLS (4)")
    assert load_ui_state(session.keep.ui_state_path) == {"nav_folded": ["tools"]}

    again = _window(qtbot, session)
    assert again.navigation.folded() == {"tools"}


def test_the_tags_panel_can_be_hidden(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    window.tags_dock.toggleViewAction().trigger()
    assert not window.tags_dock.isVisible()


def test_scan_now(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_action.trigger()
        assert not window.scan_action.isEnabled()
        assert window.busy.isVisible()
    assert window.scan_action.isEnabled()
    assert not window.busy.isVisible()
    assert window.statusBar().currentMessage() == "Scan finished: 3 new, 0 changed, 0 missing."


def test_closing_waits_for_a_running_scan(qtbot: QtBot, session: KeepSession) -> None:
    window = _window(qtbot, session)
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
        assert not window.close()  # refused while scanning
        assert "Wait for the scan" in window.statusBar().currentMessage()
    assert window.close()


def test_scan_summary() -> None:
    ok = ScanReport("a", online=True, new=2, changed=1, missing=0)
    offline = ScanReport("nas", online=False)
    broken = ScanReport("b", online=True, ingest_errors=[("x.jpg", "bad")])
    assert scan_summary([ok]) == "Scan finished: 2 new, 1 changed, 0 missing."
    assert scan_summary([ok, offline, broken]) == (
        "Scan finished: 2 new, 1 changed, 0 missing. Offline: nas. 1 file couldn't be read."
    )


# --- opening keeps from the UI ---


def test_open_in_the_background_and_remember_it(
    qtbot: QtBot, keep_dir: Path, tmp_path: Path
) -> None:
    opened: list[KeepSession] = []
    settings_path = tmp_path / "settings.toml"
    open_keep_async(None, keep_dir, Settings(), opened.append, settings_path=settings_path)
    qtbot.waitUntil(lambda: bool(opened), timeout=10_000)
    opened[0].close()
    assert opened[0].keep.config.name == "Photos"
    assert load_settings(settings_path).recent_keeps == [keep_dir]


def test_problems_are_shown(qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shown: list[str] = []
    failures: list[BaseException] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: shown.append(text))
    open_keep_async(
        None,
        tmp_path,
        Settings(),
        lambda s: None,
        settings_path=tmp_path / "s.toml",
        on_failed=failures.append,
    )
    qtbot.waitUntil(lambda: bool(failures), timeout=10_000)
    assert isinstance(failures[0], KeepError)
    assert "is not a keep" in shown[0]


CLIPS = """
from tagalot.themes.api import Entity, Theme, field

class Clip(Entity):
    length: int | None = field("Length")

class Clips(Theme):
    id, name, version = "clips", "Clips", {version}
    entities = [Clip]
"""


def test_an_older_keep_asks_before_upgrading(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "clips.py").write_text(textwrap.dedent(CLIPS.format(version=1)), encoding="utf-8")
    settings = Settings(theme_dirs=[themes])
    keep = create_keep(tmp_path / "Clips.keep", "Clips", ThemeRef("clips", 1))
    KeepSession.open(keep.dir, settings).close()  # created with theme v1
    (themes / "clips.py").write_text(textwrap.dedent(CLIPS.format(version=2)), encoding="utf-8")

    questions: list[str] = []

    def answer(
        parent: object, title: str, text: str, buttons: object
    ) -> QMessageBox.StandardButton:
        questions.append(text)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", answer)
    opened: list[KeepSession] = []
    open_keep_async(None, keep.dir, settings, opened.append, settings_path=tmp_path / "s.toml")
    qtbot.waitUntil(lambda: bool(opened), timeout=10_000)
    opened[0].close()
    assert "must be upgraded to version 2" in questions[0]
    assert opened[0].keep.config.theme.version == 2
    assert list(keep.dir.glob("keep.db.clips-v1-*.bak"))


def test_a_window_closed_before_its_data_arrives(qtbot: QtBot, session: KeepSession) -> None:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    window.deleteLater()  # gone before the saved searches load
    qtbot.wait(300)  # an exception in the late callback would fail this test
