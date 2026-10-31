"""Keep configuration's **Backups** tab (#611; DESIGN.md §4 *Backups*): scheduled backups of
the open keep on this computer, made the system's own way (``core/backup_schedule.py``).

It chooses the backup folder, how often and when, how many copies to keep, and whether
each copy holds the tag export; shows whether the keep has a schedule here and what the last
run printed; and schedules (after saying what it will add), removes, backs up now, or copies
the command. The choices are kept in the user's ``settings.toml`` (``[backups]``), since a
schedule belongs to this computer, not to the keep. The scheduler, the backup, and reading
the last run happen in workers (AGENTS.md rule 4)."""

import logging
from collections.abc import Callable
from pathlib import Path

import shiboken6
from PySide6.QtCore import QEvent, QObject, QThreadPool, QTime
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.backup import (
    BackupError,
    backup_keep,
    folder_problem,
    log_line,
    watched_folders,
)
from tagalot.core.backup_schedule import (
    DAYS,
    LastRun,
    ScheduleError,
    Scheduler,
    backup_command,
    command_line,
    last_run,
    output_file,
    when,
)
from tagalot.core.session import KeepSession
from tagalot.core.settings import WEEKDAYS, BackupPlan, save_settings
from tagalot.ui.colors import ERROR, css
from tagalot.ui.folder_picker import choose_folder
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

DEFAULT = BackupPlan("")


class BackupsTab(QWidget):
    """The Backups tab; ``saved`` takes a status-bar message, ``settings_path`` gives where
    the user's settings are saved (``None``: their ``settings.toml``)."""

    def __init__(
        self,
        session: KeepSession,
        saved: Callable[[str], None],
        settings_path: Callable[[], Path | None],
        pool: QThreadPool | None = None,
        scheduler: Scheduler | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self._saved = saved
        self._settings_path = settings_path
        self._pool = pool
        self.scheduler = scheduler or Scheduler()
        self.scheduled: bool | None = None
        """Whether the keep has a schedule here (``None`` until asked)."""
        self.busy = 0

        intro = QLabel(
            "Back this keep up on a schedule, on this computer. Each backup is a complete, "
            "checked copy of the keep's database in a dated folder, and the oldest beyond the "
            "number you keep are deleted. To restore, copy a backup's files back into the "
            "keep's folder (see the guide's Backups page)."
        )
        intro.setWordWrap(True)
        self.folder = QLineEdit()
        self.folder.setObjectName("backup_folder")
        self.folder.setPlaceholderText("A folder on another disk, or a network share")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        self.every = QComboBox()
        self.every.setObjectName("backup_every")
        self.every.addItem("Every day", "day")
        for day, name in zip(WEEKDAYS, DAYS, strict=True):
            self.every.addItem(f"Every {name}", day)
        self.time = QTimeEdit()
        self.time.setObjectName("backup_time")
        self.time.setDisplayFormat("HH:mm")
        when_row = QHBoxLayout()
        when_row.addWidget(self.every)
        when_row.addWidget(QLabel("at"))
        when_row.addWidget(self.time)
        when_row.addStretch(1)
        self.keep_last = QSpinBox()
        self.keep_last.setObjectName("backup_keep_last")
        self.keep_last.setRange(1, 999)
        self.keep_last.setSuffix(" backups")
        self.keep_last.setToolTip("Older ones are deleted; a damaged copy never counts")
        self.tags = QCheckBox("The tag export, with the items that have them")
        self.tags.setObjectName("backup_tags")
        self.tags.setToolTip(
            "Each backup also holds what Tag manager → Export tags… → Tags and the items "
            "that have them saves, which Import tags… loads into any keep"
        )
        self.status = QLabel()
        self.status.setObjectName("backup_status")
        self.status.setWordWrap(True)
        self.last = QLabel()
        self.last.setObjectName("backup_last")
        self.last.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Back up to:", folder_row)
        form.addRow("How often:", when_row)
        keep_row = QHBoxLayout()
        keep_row.addWidget(self.keep_last)
        keep_row.addStretch(1)
        form.addRow("Keep the last:", keep_row)
        form.addRow("Also save:", self.tags)
        form.addRow("Schedule:", self.status)
        form.addRow("Last backup:", self.last)
        self.error = QLabel()
        self.error.setObjectName("backup_error")
        self.error.setStyleSheet(f"color: {css(ERROR)};")
        self.error.setWordWrap(True)

        self.schedule_button = QPushButton("Schedule backups")
        self.schedule_button.setObjectName("schedule_backups")
        self.schedule_button.clicked.connect(self.schedule)
        self.remove_button = QPushButton("Remove schedule")
        self.remove_button.setObjectName("remove_schedule")
        self.remove_button.clicked.connect(self.remove)
        self.now_button = QPushButton("Back up now")
        self.now_button.setObjectName("back_up_now")
        self.now_button.setToolTip("Make a backup now, with these choices")
        self.now_button.clicked.connect(self.back_up_now)
        self.copy_button = QPushButton("Copy command")
        self.copy_button.setToolTip(
            "Copy the command a schedule runs, to set it up by hand (see the guide)"
        )
        self.copy_button.clicked.connect(self.copy_command)
        buttons = QHBoxLayout()
        buttons.addWidget(self.schedule_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        buttons.addWidget(self.now_button)
        buttons.addWidget(self.copy_button)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addLayout(buttons)
        layout.addStretch(1)
        self.show_plan(self.saved_plan())
        self._enable()
        self._looking = False
        self._look_again = False
        self._watched_window: QWidget | None = None

    def showEvent(self, event: QShowEvent) -> None:
        """Ask the system about the schedule and read the last run each time the tab is
        shown (not before: a Keep configuration opened for another tab needn't wait on
        it), and whenever its window becomes active again while the tab shows, as after
        running the task from the system's scheduler."""
        super().showEvent(event)
        window = self.window()
        if window is not self._watched_window:
            window.installEventFilter(self)
            self._watched_window = window
        self.refresh()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self._watched_window
            and event.type() == QEvent.Type.WindowActivate
            and self.isVisible()
        ):
            self.refresh()
        return super().eventFilter(watched, event)

    # --- the choices ---

    def saved_plan(self) -> BackupPlan:
        """The keep's choices on this computer, or the defaults."""
        return self.session.settings.backups.get(self.session.keep.config.id, DEFAULT)

    def show_plan(self, plan: BackupPlan) -> None:
        self.folder.setText(plan.folder)
        self.every.setCurrentIndex(max(0, self.every.findData(plan.every)))
        self.time.setTime(QTime(plan.hour, plan.minute))
        self.keep_last.setValue(plan.keep_last)
        self.tags.setChecked(plan.tags)

    def plan(self) -> BackupPlan | str:
        """The choices shown, or why they can't be used."""
        folder = self.folder.text().strip()
        if not folder:
            return "Choose a folder to back up to."
        if not Path(folder).is_absolute():
            return "The backup folder must be a full path, like E:\\Backups\\Tagalot."
        keep = self.session.keep
        watched = watched_folders(keep.config, self.session.settings)
        if problem := folder_problem(Path(folder), keep.dir, watched):
            return problem
        return BackupPlan(
            folder,
            str(self.every.currentData()),
            self.time.time().toString("HH:mm"),
            self.tags.isChecked(),
            self.keep_last.value(),
        )

    def _browse(self) -> None:
        chosen = choose_folder(
            self,
            "Back up to",
            self.session.settings,
            self._settings_path(),
            start=self.folder.text(),
        )
        if chosen:
            self.folder.setText(chosen)

    def command(self, plan: BackupPlan) -> list[str]:
        return backup_command(self.session.keep.dir, plan)

    # --- what is there ---

    def refresh(self) -> None:
        """Ask the system whether the keep has a schedule, and read the last run; one
        asking at a time (a refresh asked for meanwhile follows it)."""
        if self._looking:
            self._look_again = True
            return
        self._looking = True
        keep_id, keep_dir = self.session.keep.config.id, self.session.keep.dir
        plan = self.saved_plan()
        scheduler = self.scheduler

        def look() -> tuple[bool, LastRun | None]:
            return scheduler.installed(keep_id), last_run(plan, keep_dir) if plan.folder else None

        def done(found: tuple[bool, LastRun | None]) -> None:
            if shiboken6.isValid(self):
                self.scheduled = found[0]
                self._show(found[1])
                self._looked()

        def failed(error: BaseException) -> None:
            if shiboken6.isValid(self):
                logger.warning("Couldn't ask the scheduler about backups", exc_info=error)
                self.status.setText(f"Couldn't ask {scheduler.name}: {error}")
                self._looked()

        if self.scheduled is None:  # the first time; later, what's shown stays until then
            self.status.setText(f"Asking {scheduler.name}…")
        run_in_pool(look, on_done=done, on_error=failed, pool=self._pool)

    @property
    def asking(self) -> bool:
        """A refresh is under way."""
        return self._looking

    def _looked(self) -> None:
        self._looking = False
        if self._look_again:
            self._look_again = False
            self.refresh()

    def _show(self, last: LastRun | None) -> None:
        plan = self.saved_plan()
        if self.scheduled:
            self.status.setText(f"Backed up {when(plan)}, by {self.scheduler.name}.")
        else:
            self.status.setText("Not scheduled on this computer.")
        if last is None:
            self.last.setText("None yet." if plan.folder else "")
        else:
            mark = "" if last.ok else "Failed: "
            self.last.setText(f"{mark}{last.at:%Y-%m-%d %H:%M}: {last.text}")
        self._enable()

    def _enable(self) -> None:
        idle = self.busy == 0
        self.schedule_button.setText("Update schedule" if self.scheduled else "Schedule backups")
        for button in (self.schedule_button, self.now_button, self.copy_button):
            button.setEnabled(idle)
        self.remove_button.setEnabled(idle and bool(self.scheduled))

    # --- actions ---

    def confirm_schedule(self, text: str) -> bool:
        """Ask before adding to the system's scheduler (tests replace this)."""
        answer = QMessageBox.question(
            self,
            "Schedule backups?",
            text,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        return answer == QMessageBox.StandardButton.Ok

    def schedule(self) -> None:
        """Add (or change) the keep's schedule, after saying what will be added."""
        plan = self.plan()
        if isinstance(plan, str):
            self.error.setText(plan)
            return
        self.error.clear()
        keep = self.session.keep
        command = self.command(plan)
        verb = "change" if self.scheduled else "add"
        text = (
            f"Tagalot will {verb} {self.scheduler.entry(keep.config.id)}, which backs "
            f"{keep.config.name} up {when(plan)} by running:\n\n{command_line(command)}\n\n"
            "Remove schedule takes it away again."
        )
        if not self.confirm_schedule(text):
            return
        scheduler, description = self.scheduler, f"Tagalot: back up {keep.config.name}"

        def done(_: object) -> None:
            self._remember(plan)
            self._saved(f"Backups of {keep.config.name} scheduled {when(plan)}.")

        self._work(lambda: scheduler.install(keep.config.id, command, plan, description), done)

    def remove(self) -> None:
        """Remove the keep's schedule; the choices stay, to schedule again."""
        keep, scheduler = self.session.keep, self.scheduler

        def done(_: object) -> None:
            self._saved(f"Backups of {keep.config.name} are no longer scheduled.")

        self._work(lambda: scheduler.remove(keep.config.id), done)

    def back_up_now(self) -> None:
        """Make a backup now with the choices shown, in a worker; what it printed is
        written where a scheduled run's is, so Last backup shows it."""
        plan = self.plan()
        if isinstance(plan, str):
            self.error.setText(plan)
            return
        self.error.clear()
        keep_dir, settings = self.session.keep.dir, self.session.settings

        def back_up() -> str:
            result = backup_keep(
                keep_dir, Path(plan.folder), plan.keep_last, tags=plan.tags, settings=settings
            )
            line = log_line(result)
            output_file(plan, keep_dir).write_text(line + "\n", encoding="utf-8")
            return line if result.exit_code else f"Backed up to {result.folder}."

        def done(message: object) -> None:
            self._remember(plan)
            self._saved(str(message))

        self._work(back_up, done)

    def copy_command(self) -> None:
        plan = self.plan()
        if isinstance(plan, str):
            self.error.setText(plan)
            return
        self.error.clear()
        QApplication.clipboard().setText(command_line(self.command(plan)))
        self._saved("Copied the backup command.")

    def _remember(self, plan: BackupPlan) -> None:
        """Keep the choices in the user's settings (saved in a worker)."""
        settings, path = self.session.settings, self._settings_path()
        settings.backups[self.session.keep.config.id] = plan
        run_in_pool(lambda: save_settings(settings, path), pool=self._pool)

    def _work(self, work: Callable[[], object], then: Callable[[object], None]) -> None:
        self.busy += 1
        self._enable()

        def done(result: object) -> None:
            if not shiboken6.isValid(self):
                return
            self.busy -= 1
            then(result)
            self.refresh()

        def failed(error: BaseException) -> None:
            if not shiboken6.isValid(self):
                return
            self.busy -= 1
            if not isinstance(error, ScheduleError | BackupError | OSError):
                logger.error("A backup action failed", exc_info=error)
            self.error.setText(str(error))
            self.refresh()

        run_in_pool(work, on_done=done, on_error=failed, pool=self._pool)
