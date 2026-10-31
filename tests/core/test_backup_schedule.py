"""Scheduled backups the system's own way (#611): what each scheduler is given, and the
commands run, through a fake runner and a home folder in ``tmp_path`` (never the real
scheduler)."""

import plistlib
import shlex
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

from tagalot.core import backup_schedule
from tagalot.core.backup_schedule import (
    CRON,
    LAUNCHD,
    SYSTEMD,
    WINDOWS,
    ScheduleError,
    Scheduler,
    backup_command,
    command_line,
    cron_lines,
    last_run,
    launchd_plist,
    output_file,
    program,
    systemd_units,
    task_xml,
    when,
    without_backup,
)
from tagalot.core.settings import BackupPlan

KEEP_ID = uuid.UUID("0b6e3c1e-6f0a-4b54-9a8e-2b2d7f1c9d41")
TASK = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
KEEP = Path("D:/Keeps/Old Movies.keep")
DAILY = BackupPlan("E:/Backups/Tagalot", "day", "03:00", True, 14)
WEEKLY = BackupPlan("E:/Backups/100% Tagalot", "monday", "21:30", False, 4)
START = ["C:/Program Files/Tagalot/tagalot-cli.exe"]


class FakeSystem:
    """Records commands; answers them like the scheduler would."""

    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.crontab: str | None = None
        self.tasks: dict[str, str] = {}
        self.fail = fail

    def __call__(self, args: Sequence[str], stdin: str | None) -> tuple[int, str]:
        args = list(args)
        self.calls.append(args)
        if self.fail is not None and args[0] == self.fail:
            return 1, "Access is denied."
        if args[:2] == ["crontab", "-l"]:
            return (0, self.crontab) if self.crontab is not None else (1, "no crontab for you")
        if args[:2] == ["crontab", "-"]:
            self.crontab = stdin
        if args[:2] == ["schtasks", "/Create"]:
            xml = Path(args[args.index("/XML") + 1])
            self.tasks[args[3]] = xml.read_text(encoding="utf-16")
        if args[:2] == ["schtasks", "/Query"]:
            return (0, "") if args[3] in self.tasks else (1, "ERROR: cannot find the file")
        if args[:2] == ["schtasks", "/Delete"]:
            del self.tasks[args[3]]
        return 0, ""


def test_the_command() -> None:
    command = backup_command(KEEP, DAILY, START)
    assert command == [
        *START,
        "--backup",
        str(KEEP),
        "--to",
        "E:/Backups/Tagalot",
        "--keep-last",
        "14",
        "--tags",
        "--output",
        str(Path("E:/Backups/Tagalot") / "Old Movies last run.txt"),
    ]
    assert "--tags" not in backup_command(KEEP, WEEKLY, START)
    windows = command_line(command, "win32")
    assert windows.startswith('"C:/Program Files/Tagalot/tagalot-cli.exe" --backup "D:')
    assert shlex.split(command_line(command, "linux")) == command
    assert when(DAILY) == "every day at 03:00"
    assert when(WEEKLY) == "every Monday at 21:30"


def test_how_tagalot_is_started(monkeypatch: pytest.MonkeyPatch) -> None:
    assert program() == [sys.executable, "-m", "tagalot"]  # from source
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/Tagalot/tagalot.exe")
    assert program("win32") == [str(Path("C:/Tagalot/tagalot-cli.exe"))]
    monkeypatch.setattr(sys, "executable", "/opt/tagalot/tagalot")
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert program("linux") == ["/opt/tagalot/tagalot"]
    monkeypatch.setenv("APPIMAGE", "/home/me/Tagalot.AppImage")
    assert program("linux") == ["/home/me/Tagalot.AppImage"]


def test_a_task_scheduler_task() -> None:
    command = backup_command(KEEP, WEEKLY, START)
    root = ET.fromstring(task_xml(command, WEEKLY, "Back up <Old Movies>").split("?>", 1)[1])
    assert root.findtext(f"{TASK}RegistrationInfo/{TASK}Description") == "Back up <Old Movies>"
    trigger = root.find(f"{TASK}Triggers/{TASK}CalendarTrigger")
    assert trigger is not None
    assert trigger.findtext(f"{TASK}StartBoundary") == "2026-01-01T21:30:00"
    assert trigger.find(f"{TASK}ScheduleByWeek/{TASK}DaysOfWeek/{TASK}Monday") is not None
    assert root.findtext(f"{TASK}Settings/{TASK}StartWhenAvailable") == "true"
    exec_ = root.find(f"{TASK}Actions/{TASK}Exec")
    assert exec_ is not None
    assert exec_.findtext(f"{TASK}Command") == START[0]
    assert exec_.findtext(f"{TASK}Arguments") == subprocess.list2cmdline(command[1:])
    daily = ET.fromstring(task_xml(command, DAILY, "x").split("?>", 1)[1])
    assert daily.find(f"{TASK}Triggers/{TASK}CalendarTrigger/{TASK}ScheduleByDay") is not None


def test_a_launchd_agent() -> None:
    command = backup_command(KEEP, WEEKLY, ["/Applications/Tagalot.app/Contents/MacOS/tagalot"])
    agent = plistlib.loads(launchd_plist("io.x.backup", command, WEEKLY))
    assert agent["ProgramArguments"] == command
    assert agent["StartCalendarInterval"] == {"Hour": 21, "Minute": 30, "Weekday": 1}
    sunday = BackupPlan("/b", "sunday", "03:00")
    assert (
        plistlib.loads(launchd_plist("x", command, sunday))["StartCalendarInterval"]["Weekday"] == 0
    )
    daily = plistlib.loads(launchd_plist("x", command, DAILY))["StartCalendarInterval"]
    assert "Weekday" not in daily


def test_systemd_units() -> None:
    command = ["/opt/Tagalot.AppImage", "--backup", "/home/me/Old Movies.keep", "--to", "/b/100%"]
    service, timer = systemd_units(command, WEEKLY, "Tagalot backup")
    assert (
        'ExecStart="/opt/Tagalot.AppImage" "--backup" "/home/me/Old Movies.keep" "--to" "/b/100%%"'
        in service
    )
    assert "Type=oneshot" in service
    assert "OnCalendar=Mon *-*-* 21:30:00" in timer
    assert "Persistent=true" in timer
    assert "OnCalendar=*-*-* 03:00:00" in systemd_units(command, DAILY, "x")[1]


def test_crontab_lines() -> None:
    command = backup_command(Path("/home/me/Old Movies.keep"), WEEKLY, ["/opt/Tagalot.AppImage"])
    marker, entry = cron_lines(KEEP_ID, command, WEEKLY)
    assert str(KEEP_ID) in marker
    assert entry.startswith("30 21 * * 1 /opt/Tagalot.AppImage --backup ")
    assert "100\\% Tagalot" in entry  # cron would end the command at a bare %
    assert cron_lines(KEEP_ID, command, BackupPlan("/b", "sunday"))[1].startswith("0 3 * * 0 ")
    crontab = "\n".join(["MAILTO=me", marker, entry, "0 1 * * * other"])
    assert without_backup(crontab, KEEP_ID) == ["MAILTO=me", "0 1 * * * other"]


def test_windows_install_replace_and_remove(tmp_path: Path) -> None:
    system = FakeSystem()
    scheduler = Scheduler(WINDOWS, system, tmp_path)
    assert not scheduler.installed(KEEP_ID)
    scheduler.install(KEEP_ID, backup_command(KEEP, DAILY, START), DAILY, "Back up")
    name = "\\Tagalot\\Backup 0b6e3c1e-6f0a-4b54-9a8e-2b2d7f1c9d41"
    assert system.calls[-1][:4] == ["schtasks", "/Create", "/TN", name]
    assert system.calls[-1][-1] == "/F"  # replaces the keep's task
    assert "<DaysInterval>1</DaysInterval>" in system.tasks[name]
    assert scheduler.installed(KEEP_ID)
    assert name in scheduler.entry(KEEP_ID)
    scheduler.remove(KEEP_ID)
    assert not scheduler.installed(KEEP_ID)
    scheduler.remove(KEEP_ID)  # nothing to remove: no error
    assert ["schtasks", "/Delete", "/TN", name, "/F"] in system.calls


def test_a_refusal_is_reported(tmp_path: Path) -> None:
    scheduler = Scheduler(WINDOWS, FakeSystem(fail="schtasks"), tmp_path)
    with pytest.raises(ScheduleError, match=r"schtasks said: Access is denied."):
        scheduler.install(KEEP_ID, START, DAILY, "x")


def test_cron_install_and_remove(tmp_path: Path) -> None:
    system = FakeSystem()
    system.crontab = "MAILTO=me\n0 1 * * * other\n"
    scheduler = Scheduler(CRON, system, tmp_path)
    command = backup_command(KEEP, DAILY, ["/opt/tagalot"])
    scheduler.install(KEEP_ID, command, DAILY, "x")
    scheduler.install(KEEP_ID, command, WEEKLY, "x")  # replaced, not added twice
    assert system.crontab is not None
    lines = system.crontab.splitlines()
    assert lines[:2] == ["MAILTO=me", "0 1 * * * other"]
    assert len(lines) == 4
    assert lines[3].startswith("30 21 * * 1 ")
    assert scheduler.installed(KEEP_ID)
    scheduler.remove(KEEP_ID)
    assert system.crontab == "MAILTO=me\n0 1 * * * other\n"
    assert not scheduler.installed(KEEP_ID)


def test_cron_with_no_crontab_yet(tmp_path: Path) -> None:
    system = FakeSystem()
    scheduler = Scheduler(CRON, system, tmp_path)
    scheduler.install(KEEP_ID, ["/opt/tagalot"], DAILY, "x")
    assert system.crontab is not None
    assert system.crontab.splitlines()[1] == "0 3 * * * /opt/tagalot"


def test_systemd_install_and_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    system = FakeSystem()
    scheduler = Scheduler(SYSTEMD, system, tmp_path)
    scheduler.install(KEEP_ID, ["/opt/tagalot"], DAILY, "Back up")
    units = tmp_path / ".config" / "systemd" / "user"
    timer = f"tagalot-backup-{KEEP_ID}.timer"
    assert (units / timer).is_file()
    assert (units / f"tagalot-backup-{KEEP_ID}.service").is_file()
    assert ["systemctl", "--user", "enable", "--now", timer] in system.calls
    assert scheduler.installed(KEEP_ID)
    scheduler.remove(KEEP_ID)
    assert list(units.iterdir()) == []
    assert ["systemctl", "--user", "disable", "--now", timer] in system.calls


def test_launchd_install_and_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_schedule, "_uid", lambda: 501)
    system = FakeSystem()
    scheduler = Scheduler(LAUNCHD, system, tmp_path)
    scheduler.install(KEEP_ID, ["/Applications/Tagalot.app/Contents/MacOS/tagalot"], DAILY, "x")
    plist = (
        tmp_path / "Library" / "LaunchAgents" / f"io.github.torstees.tagalot.backup.{KEEP_ID}.plist"
    )
    assert plistlib.loads(plist.read_bytes())["Label"].endswith(str(KEEP_ID))
    assert ["launchctl", "bootstrap", "gui/501", str(plist)] in system.calls
    assert scheduler.installed(KEEP_ID)
    scheduler.remove(KEEP_ID)
    assert not plist.exists()
    assert ["launchctl", "bootout", f"gui/501/io.github.torstees.tagalot.backup.{KEEP_ID}"] in (
        system.calls
    )


def test_the_last_run(tmp_path: Path) -> None:
    plan = BackupPlan(str(tmp_path), "day")
    assert last_run(plan, KEEP) is None
    output_file(plan, KEEP).write_text(
        "2026-10-31 03:00:02  ok  Old Movies.keep -> 2026-10-31_0300 (12.4 MB)\n   E:/x\n",
        encoding="utf-8",
    )
    run = last_run(plan, KEEP)
    assert run is not None
    assert run.ok
    assert run.text.endswith("(12.4 MB)")
    assert abs((datetime.now() - run.at).total_seconds()) < 60
    output_file(plan, KEEP).write_text("error: D:/Keeps isn't a keep\nusage: …\n", encoding="utf-8")
    failed = last_run(plan, KEEP)
    assert failed is not None
    assert not failed.ok
    output_file(plan, KEEP).write_text(
        "2026-10-31 03:00:02  ok  Old Movies.keep -> x (1 KB); tags FAILED: no theme\n",
        encoding="utf-8",
    )
    tags_failed = last_run(plan, KEEP)
    assert tags_failed is not None
    assert not tags_failed.ok
