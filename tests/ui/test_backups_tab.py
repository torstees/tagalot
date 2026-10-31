"""Keep configuration's Backups tab (#611), on the demo keep, with a scheduler whose system
is a fake (``tests/core/test_backup_schedule.py``): never the real one."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QTime
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from tagalot.core.backup_schedule import WINDOWS, Scheduler, task_name
from tagalot.core.session import KeepSession
from tagalot.core.settings import BackupPlan, Settings, load_settings
from tagalot.ui.backups_tab import BackupsTab
from tagalot.ui.keep_config import KeepConfigWindow
from tests.core.test_backup_schedule import FakeSystem

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_demo(tmp_path / "scratch"), Settings()) as session:
        yield session


def _tab(
    qtbot: QtBot, session: KeepSession, tmp_path: Path, fail: str | None = None
) -> tuple[KeepConfigWindow, BackupsTab, FakeSystem]:
    config = KeepConfigWindow(session)
    qtbot.addWidget(config)
    config.settings_path = tmp_path / "settings.toml"
    system = FakeSystem(fail)
    tab = config.backups_tab
    tab.scheduler = Scheduler(WINDOWS, system, tmp_path)
    assert system.calls == []  # nothing asked before the tab is shown
    config.show()
    config.tabs.setCurrentIndex(config.tabs.count() - 1)
    qtbot.waitUntil(lambda: tab.scheduled is not None and not tab.asking, timeout=5000)
    return config, tab, system


def _settle(qtbot: QtBot, tab: BackupsTab) -> None:
    qtbot.waitUntil(lambda: tab.busy == 0 and not tab.asking, timeout=10000)


def test_scheduling_changing_and_removing(
    qtbot: QtBot, session: KeepSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, tab, system = _tab(qtbot, session, tmp_path)
    assert config.tabs.tabText(config.tabs.currentIndex()) == "Backups"
    assert tab.status.text() == "Not scheduled on this computer."
    assert not tab.remove_button.isEnabled()
    asked: list[str] = []

    def confirm(text: str) -> bool:
        asked.append(text)
        return True

    monkeypatch.setattr(tab, "confirm_schedule", confirm)
    folder = tmp_path / "backups"
    tab.folder.setText(str(folder))
    tab.every.setCurrentIndex(tab.every.findData("monday"))
    tab.time.setTime(QTime(21, 30))
    tab.keep_last.setValue(4)
    tab.tags.setChecked(False)
    tab.schedule_button.click()
    _settle(qtbot, tab)
    name = task_name(session.keep.config.id)
    assert name in asked[0]
    assert "every Monday at 21:30" in asked[0]
    assert "--keep-last 4" in asked[0]
    assert name in system.tasks
    assert "<Monday />" in system.tasks[name]
    plan = BackupPlan(str(folder), "monday", "21:30", False, 4)
    assert session.settings.backups[session.keep.config.id] == plan
    qtbot.waitUntil(lambda: (tmp_path / "settings.toml").is_file(), timeout=5000)
    assert load_settings(tmp_path / "settings.toml").backups == {session.keep.config.id: plan}
    assert tab.status.text() == "Backed up every Monday at 21:30, by Windows Task Scheduler."
    assert tab.schedule_button.text() == "Update schedule"
    assert tab.remove_button.isEnabled()
    assert tab.last.text() == "None yet."

    tab.every.setCurrentIndex(tab.every.findData("day"))
    tab.schedule_button.click()
    _settle(qtbot, tab)
    assert asked[1].startswith("Tagalot will change ")
    assert "<DaysInterval>1</DaysInterval>" in system.tasks[name]

    tab.remove_button.click()
    _settle(qtbot, tab)
    assert system.tasks == {}
    assert tab.status.text() == "Not scheduled on this computer."
    assert tab.folder.text() == str(folder)  # the choices stay, to schedule again


def test_nothing_is_added_without_a_yes(
    qtbot: QtBot, session: KeepSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, tab, system = _tab(qtbot, session, tmp_path)
    monkeypatch.setattr(tab, "confirm_schedule", lambda text: False)
    tab.folder.setText(str(tmp_path / "backups"))
    tab.schedule_button.click()
    assert system.tasks == {}
    assert session.keep.config.id not in session.settings.backups


def test_folders_that_cant_be_used(
    qtbot: QtBot, session: KeepSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, tab, system = _tab(qtbot, session, tmp_path)
    monkeypatch.setattr(tab, "confirm_schedule", lambda text: pytest.fail("asked"))
    tab.schedule_button.click()
    assert tab.error.text() == "Choose a folder to back up to."
    tab.folder.setText("backups")
    tab.schedule_button.click()
    assert "full path" in tab.error.text()
    root = session.keep.config.roots[0]
    tab.folder.setText(str(Path(session.root_path(root.id)) / "backups"))
    tab.schedule_button.click()
    assert f"can't be in {root.name}, a folder this keep watches" in tab.error.text()
    tab.folder.setText(str(session.keep.dir / "backups"))
    tab.back_up_now()
    assert "inside the keep's folder" in tab.error.text()
    assert system.tasks == {}


def test_a_refusal_is_shown(
    qtbot: QtBot, session: KeepSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, tab, _ = _tab(qtbot, session, tmp_path, fail="schtasks")
    monkeypatch.setattr(tab, "confirm_schedule", lambda text: True)
    tab.folder.setText(str(tmp_path / "backups"))
    tab.schedule_button.click()
    qtbot.waitUntil(lambda: tab.error.text() != "", timeout=5000)
    _settle(qtbot, tab)
    assert tab.error.text() == "schtasks said: Access is denied."
    assert session.keep.config.id not in session.settings.backups


def test_back_up_now_and_copy_command(qtbot: QtBot, session: KeepSession, tmp_path: Path) -> None:
    config, tab, _ = _tab(qtbot, session, tmp_path)
    messages: list[str] = []
    config.changed.connect(messages.append)
    folder = tmp_path / "backups"
    tab.folder.setText(str(folder))
    tab.back_up_now()
    _settle(qtbot, tab)
    assert messages[-1].startswith(f"Backed up to {folder / 'Demo'}")
    assert len(list((folder / "Demo").iterdir())) == 1
    assert (folder / "Demo last run.txt").is_file()
    assert "  ok  Demo.keep -> " in tab.last.text()
    assert not tab.last.text().startswith("Failed")
    tab.copy_button.click()
    copied = QApplication.clipboard().text()
    assert "--backup" in copied
    assert str(session.keep.dir) in copied
    assert messages[-1] == "Copied the backup command."


def test_the_last_backup_is_read_again(
    qtbot: QtBot, session: KeepSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, tab, _ = _tab(qtbot, session, tmp_path)
    folder = tmp_path / "backups"
    tab.folder.setText(str(folder))
    tab.back_up_now()
    _settle(qtbot, tab)
    run = folder / "Demo last run.txt"
    line = "2026-11-02 03:00:01  ok  Demo.keep -> 2026-11-02_0300 (480.0 KB)"
    run.write_text(line + "\n", encoding="utf-8")  # a scheduled run, while the tab was open
    config.tabs.setCurrentIndex(0)
    config.tabs.setCurrentIndex(config.tabs.count() - 1)  # back to the tab: read again
    qtbot.waitUntil(lambda: tab.last.text().endswith("(480.0 KB)"), timeout=5000)
    _settle(qtbot, tab)
    run.write_text("error: something went wrong\n", encoding="utf-8")
    QApplication.sendEvent(config, QEvent(QEvent.Type.WindowActivate))  # back to the window
    qtbot.waitUntil(lambda: tab.last.text().startswith("Failed: "), timeout=5000)
    assert tab.last.text().endswith("error: something went wrong")
    _settle(qtbot, tab)
