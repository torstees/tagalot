"""Scheduled backups, made the system's own way (#611; DESIGN.md §4 *Backups*).

Keep configuration's Backups tab schedules one command, ``tagalot --backup`` with the
choices of a :class:`~tagalot.core.settings.BackupPlan`, in:

- **Windows:** Task Scheduler, a task ``\\Tagalot\\Backup <keep id>`` made from an XML
  definition (``schtasks /Create /XML``), which has no limit on the command's length and
  no quoting of its own; a missed run starts when the computer is next on.
- **macOS:** a launchd user agent, ``~/Library/LaunchAgents/<label>.plist``.
- **Linux:** a systemd user timer and service in ``~/.config/systemd/user``, or a line in
  the user's crontab where there's no systemd.

These are the user's own configuration, never files under a root. Everything here builds
text, or runs the system's commands through a :data:`Run` callable that tests replace, so
nothing here needs the system it is about."""

import logging
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from tagalot.core.backup import keep_name
from tagalot.core.settings import BackupPlan

logger = logging.getLogger(__name__)

WINDOWS, LAUNCHD, SYSTEMD, CRON = "windows", "launchd", "systemd", "cron"
DESCRIBED = {
    WINDOWS: "Windows Task Scheduler",
    LAUNCHD: "launchd (a user agent)",
    SYSTEMD: "a systemd user timer",
    CRON: "your crontab",
}
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

Run = Callable[[Sequence[str], str | None], tuple[int, str]]
"""Run a command (with text on its standard input): its exit code and what it printed."""


class ScheduleError(Exception):
    """The system refused a change to the schedule: the message says what it said."""


def run_command(args: Sequence[str], stdin: str | None = None) -> tuple[int, str]:
    """Run ``args`` without a console window; its exit code and output (127 when it
    can't be started)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            list(args),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return done.returncode, (done.stdout + done.stderr).strip()


def scheduler_kind(platform: str = sys.platform) -> str:
    """Which scheduler this computer uses."""
    if platform == "win32":
        return WINDOWS
    if platform == "darwin":
        return LAUNCHD
    systemd = shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()
    return SYSTEMD if systemd else CRON


def program(platform: str = sys.platform) -> list[str]:
    """How to start Tagalot's commands on this computer: the console twin beside a
    Windows build (#610), the AppImage or the app's executable, or this Python from
    source."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        if platform == "win32":
            return [str(exe.with_name("tagalot-cli.exe"))]
        appimage = os.environ.get("APPIMAGE")
        return [appimage] if appimage else [sys.executable]
    return [sys.executable, "-m", "tagalot"]


def output_file(plan: BackupPlan, keep_dir: Path) -> Path:
    """Where a scheduled run writes what it printed (``--output``), errors included."""
    return Path(plan.folder) / f"{keep_name(keep_dir)} last run.txt"


def backup_command(
    keep_dir: Path, plan: BackupPlan, start: Sequence[str] | None = None
) -> list[str]:
    """The command a schedule runs for ``plan``."""
    command = [*(start or program()), "--backup", str(keep_dir), "--to", plan.folder]
    command += ["--keep-last", str(plan.keep_last)]
    if plan.tags:
        command.append("--tags")
    return [*command, "--output", str(output_file(plan, keep_dir))]


def command_line(command: Sequence[str], platform: str = sys.platform) -> str:
    """The command as one line to paste into this system's shell or scheduler."""
    if platform == "win32":
        return subprocess.list2cmdline(list(command))
    return shlex.join(command)


def when(plan: BackupPlan) -> str:
    """``every day at 03:00``, ``every Monday at 21:30``."""
    day = "day" if plan.weekday is None else DAYS[plan.weekday]
    return f"every {day} at {plan.time}"


@dataclass(frozen=True)
class LastRun:
    """What the last scheduled run printed, from its ``--output`` file."""

    at: datetime
    text: str

    @property
    def ok(self) -> bool:
        return self.text.split("  ")[1:2] == ["ok"] and "FAILED" not in self.text


def last_run(plan: BackupPlan, keep_dir: Path) -> LastRun | None:
    """The last scheduled (or command-line) run's output, when there is one."""
    path = output_file(plan, keep_dir)
    try:
        text = path.read_text(encoding="utf-8").strip()
        at = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None
    return LastRun(at, text.splitlines()[0] if text else "(it printed nothing)")


# --- what each scheduler is given ---


def task_name(keep_id: uuid.UUID) -> str:
    return f"\\Tagalot\\Backup {keep_id}"


def agent_label(keep_id: uuid.UUID) -> str:
    return f"io.github.torstees.tagalot.backup.{keep_id}"


def unit_name(keep_id: uuid.UUID) -> str:
    return f"tagalot-backup-{keep_id}"


def _uid() -> int:
    """The user's id, for launchd's ``gui/<uid>`` domain (macOS only)."""
    getuid: Callable[[], int] = getattr(os, "getuid")  # noqa: B009 (absent on Windows)
    return getuid()


def cron_marker(keep_id: uuid.UUID) -> str:
    return f"# Tagalot: backup of keep {keep_id} (Keep configuration, Backups)"


def task_xml(command: Sequence[str], plan: BackupPlan, description: str) -> str:
    """A Task Scheduler task running ``command`` as the user, when ``plan`` says; a missed
    run starts when the computer is next on."""
    if plan.weekday is None:
        schedule = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    else:
        schedule = (
            f"<ScheduleByWeek><DaysOfWeek><{DAYS[plan.weekday]} /></DaysOfWeek>"
            "<WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
        )
    arguments = subprocess.list2cmdline(list(command[1:]))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{plan.time}:00</StartBoundary>
      <Enabled>true</Enabled>
      {schedule}
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT4H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command[0])}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def launchd_plist(label: str, command: Sequence[str], plan: BackupPlan) -> bytes:
    """A launchd user agent running ``command`` when ``plan`` says."""
    interval: dict[str, int] = {"Hour": plan.hour, "Minute": plan.minute}
    if plan.weekday is not None:
        interval["Weekday"] = (plan.weekday + 1) % 7  # launchd counts from Sunday, 0
    return plistlib.dumps(
        {"Label": label, "ProgramArguments": list(command), "StartCalendarInterval": interval}
    )


def systemd_units(command: Sequence[str], plan: BackupPlan, description: str) -> tuple[str, str]:
    """A systemd user service running ``command``, and its timer; a missed run starts when
    the computer is next on (``Persistent``)."""
    day = "" if plan.weekday is None else f"{DAYS[plan.weekday][:3]} "
    service = (
        f"[Unit]\nDescription={description}\n\n"
        f"[Service]\nType=oneshot\nExecStart={' '.join(_systemd_quote(a) for a in command)}\n"
    )
    timer = (
        f"[Unit]\nDescription={description}\n\n"
        f"[Timer]\nOnCalendar={day}*-*-* {plan.time}:00\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def _systemd_quote(arg: str) -> str:
    """One argument of ``ExecStart``: quoted, with systemd's specifiers (``%``) and
    variables (``$``) written literally."""
    arg = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{arg}"'


def cron_lines(keep_id: uuid.UUID, command: Sequence[str], plan: BackupPlan) -> list[str]:
    """The crontab lines: a marker naming the keep, then the entry."""
    day = "*" if plan.weekday is None else str((plan.weekday + 1) % 7)  # cron: Sunday is 0
    line = shlex.join(command).replace("%", "\\%")  # cron ends a command at a bare %
    return [cron_marker(keep_id), f"{plan.minute} {plan.hour} * * {day} {line}"]


def without_backup(crontab: str, keep_id: uuid.UUID) -> list[str]:
    """The crontab's lines without the keep's marker and the entry after it."""
    lines, kept, skip = crontab.splitlines(), [], False
    for line in lines:
        if skip:
            skip = False
            continue
        if line.strip() == cron_marker(keep_id):
            skip = True
            continue
        kept.append(line)
    return kept


# --- the system's scheduler ---


@dataclass
class Scheduler:
    """This computer's scheduler, through :data:`Run` (replaced in tests) and the user's
    home folder (where launchd agents and systemd units go)."""

    kind: str = field(default_factory=scheduler_kind)
    run: Run | None = None
    """Runs the system's commands; :func:`run_command` when ``None`` (looked up when
    called, so tests can stop every scheduler from reaching the real one)."""
    home: Path = field(default_factory=Path.home)

    @property
    def name(self) -> str:
        return DESCRIBED[self.kind]

    def entry(self, keep_id: uuid.UUID) -> str:
        """What the schedule adds, named for the person asked to confirm it."""
        if self.kind == WINDOWS:
            return f"the task {task_name(keep_id)} in Windows Task Scheduler"
        if self.kind == LAUNCHD:
            return f"the launchd agent {self._plist(keep_id)}"
        if self.kind == SYSTEMD:
            return f"the systemd user timer {unit_name(keep_id)}.timer, in {self._units()}"
        return "a line in your crontab"

    def installed(self, keep_id: uuid.UUID) -> bool:
        """Whether the keep has a schedule on this computer."""
        if self.kind == WINDOWS:
            return self._call(["schtasks", "/Query", "/TN", task_name(keep_id)], None)[0] == 0
        if self.kind == LAUNCHD:
            return self._plist(keep_id).is_file()
        if self.kind == SYSTEMD:
            return (self._units() / f"{unit_name(keep_id)}.timer").is_file()
        return cron_marker(keep_id) in self._crontab()

    def install(
        self, keep_id: uuid.UUID, command: Sequence[str], plan: BackupPlan, description: str
    ) -> None:
        """Add the schedule, or replace the keep's. :class:`ScheduleError` if the system
        refuses."""
        if self.kind == WINDOWS:
            self._install_task(keep_id, command, plan, description)
        elif self.kind == LAUNCHD:
            plist = self._plist(keep_id)
            plist.parent.mkdir(parents=True, exist_ok=True)
            self._call(["launchctl", "bootout", f"gui/{_uid()}/{agent_label(keep_id)}"], None)
            plist.write_bytes(launchd_plist(agent_label(keep_id), command, plan))
            self._check(["launchctl", "bootstrap", f"gui/{_uid()}", str(plist)])
        elif self.kind == SYSTEMD:
            service, timer = systemd_units(command, plan, description)
            units = self._units()
            units.mkdir(parents=True, exist_ok=True)
            (units / f"{unit_name(keep_id)}.service").write_text(service, encoding="utf-8")
            (units / f"{unit_name(keep_id)}.timer").write_text(timer, encoding="utf-8")
            self._check(["systemctl", "--user", "daemon-reload"])
            self._check(["systemctl", "--user", "enable", "--now", f"{unit_name(keep_id)}.timer"])
        else:
            lines = without_backup(self._crontab(), keep_id) + cron_lines(keep_id, command, plan)
            self._check(["crontab", "-"], "\n".join(lines) + "\n")

    def remove(self, keep_id: uuid.UUID) -> None:
        """Remove the keep's schedule, if it has one."""
        if self.kind == WINDOWS:
            if self.installed(keep_id):
                self._check(["schtasks", "/Delete", "/TN", task_name(keep_id), "/F"])
        elif self.kind == LAUNCHD:
            self._call(["launchctl", "bootout", f"gui/{_uid()}/{agent_label(keep_id)}"], None)
            self._plist(keep_id).unlink(missing_ok=True)
        elif self.kind == SYSTEMD:
            timer = f"{unit_name(keep_id)}.timer"
            self._call(["systemctl", "--user", "disable", "--now", timer], None)
            for end in (".timer", ".service"):
                (self._units() / f"{unit_name(keep_id)}{end}").unlink(missing_ok=True)
            self._check(["systemctl", "--user", "daemon-reload"])
        else:
            crontab = self._crontab()
            if cron_marker(keep_id) in crontab:
                kept = without_backup(crontab, keep_id)
                self._check(["crontab", "-"], "\n".join(kept) + "\n" if kept else "")

    def _install_task(
        self, keep_id: uuid.UUID, command: Sequence[str], plan: BackupPlan, description: str
    ) -> None:
        with tempfile.TemporaryDirectory() as folder:
            xml = Path(folder) / "task.xml"
            xml.write_text(task_xml(command, plan, description), encoding="utf-16")
            self._check(["schtasks", "/Create", "/TN", task_name(keep_id), "/XML", str(xml), "/F"])

    def _call(self, args: Sequence[str], stdin: str | None) -> tuple[int, str]:
        return (self.run or run_command)(args, stdin)

    def _check(self, args: Sequence[str], stdin: str | None = None) -> None:
        code, said = self._call(args, stdin)
        if code != 0:
            logger.warning("%s failed (%s): %s", args[0], code, said)
            raise ScheduleError(f"{args[0]} said: {said or f'exit code {code}'}")

    def _crontab(self) -> str:
        code, text = self._call(["crontab", "-l"], None)
        return text if code == 0 else ""  # 1: no crontab yet

    def _plist(self, keep_id: uuid.UUID) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{agent_label(keep_id)}.plist"

    def _units(self) -> Path:
        config = os.environ.get("XDG_CONFIG_HOME")
        base = Path(config) if config and self.home == Path.home() else self.home / ".config"
        return base / "systemd" / "user"
