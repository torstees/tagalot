"""The Keep configuration window: the keep's roots (DESIGN.md §12 "Keep configuration").

A window of its own (Keep → Configure keep…), one per main window. The roots are listed on
the left; the selected one's name, folder, this computer's folder, exclude patterns, and
status are on the right, with Scan now, Stop watching / Watch again, and Remove….

Every change is checked and saved as soon as it's made (``keep.toml``, or ``settings.toml``
for this computer's folder), in a worker; a bad value is explained under the form and the
field shows the saved value again. A changed folder or exclude list offers a scan.
"""

import html
import logging
from collections.abc import Callable
from pathlib import Path

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.keep import KeepError, RootConfig
from tagalot.core.root_admin import (
    RemovalCounts,
    RootStatus,
    add_root,
    edit_root,
    removal_counts,
    root_statuses,
)
from tagalot.core.session import KeepSession
from tagalot.core.settings import save_settings
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

KEEP, DELETE = "keep", "delete"
"""The two ways to remove a root."""

_ROOT_ID = Qt.ItemDataRole.UserRole


class KeepConfigWindow(QWidget):
    """Edit an open keep's roots; see the module docstring.

    Signals: :attr:`changed` after anything was saved (pages show root names and file
    statuses), :attr:`scan_requested` with the roots to scan.
    """

    changed = Signal(str)
    """Something was saved; a status-bar message."""
    scan_requested = Signal(list)
    """Scan these root ids now."""

    def __init__(
        self, session: KeepSession, pool: QThreadPool | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.session = session
        self._pool = pool
        self.settings_path: Path | None = None
        """Where this computer's folders are saved (``None``: the user's ``settings.toml``)."""
        self.statuses: dict[str, RootStatus] = {}
        self.busy = 0
        self.setWindowTitle(f"Configure {session.keep.config.name}")
        self.resize(820, 520)

        self.roots = QListWidget()
        self.roots.setObjectName("roots")
        self.roots.currentItemChanged.connect(lambda *_: self._show_root())
        self.add_button = QPushButton("Add folder…")
        self.add_button.setToolTip("Watch another folder: its files become part of this keep")
        self.add_button.clicked.connect(self._choose_new_root)
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Watched folders</b>"))
        left.addWidget(self.roots, 1)
        left.addWidget(self.add_button)

        self.name = QLineEdit()
        self.name.editingFinished.connect(lambda: self._edit(name=self.name.text()))
        self.path = QLineEdit()
        self.path.editingFinished.connect(lambda: self._edit_path(self.path.text()))
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: self._browse(self._edit_path))
        path_row = QHBoxLayout()
        path_row.addWidget(self.path, 1)
        path_row.addWidget(browse)
        self.local = QLineEdit()
        self.local.setPlaceholderText("The folder above")
        self.local.setToolTip(
            "Where this computer reaches the folder, if not at the path above (another drive "
            "letter or mount). Saved for you on this computer only."
        )
        self.local.editingFinished.connect(lambda: self._set_local(self.local.text()))
        local_browse = QPushButton("Browse…")
        local_browse.clicked.connect(lambda: self._browse(self._set_local))
        local_clear = QPushButton("Clear")
        local_clear.clicked.connect(lambda: self._set_local(""))
        local_row = QHBoxLayout()
        local_row.addWidget(self.local, 1)
        local_row.addWidget(local_browse)
        local_row.addWidget(local_clear)
        self.exclude = QPlainTextEdit()
        self.exclude.setPlaceholderText("One pattern per line, like **/cache/**")
        self.exclude.setToolTip(
            "Files and folders matching these patterns are skipped when scanning. "
            "Saved when you leave the box."
        )
        self.exclude.installEventFilter(self)
        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setTextFormat(Qt.TextFormat.RichText)
        self.status.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Name:", self.name)
        form.addRow("Folder:", path_row)
        form.addRow("On this computer:", local_row)
        form.addRow("Skip:", self.exclude)
        form.addRow("Status:", self.status)

        self.scan_button = QPushButton("Scan now")
        self.scan_button.clicked.connect(self._scan)
        self.watch_button = QPushButton()
        self.watch_button.clicked.connect(self._toggle_watched)
        self.remove_button = QPushButton("Remove…")
        self.remove_button.clicked.connect(self._remove)
        buttons = QHBoxLayout()
        buttons.addWidget(self.scan_button)
        buttons.addWidget(self.watch_button)
        buttons.addStretch(1)
        buttons.addWidget(self.remove_button)
        self.error = QLabel()
        self.error.setObjectName("error")
        self.error.setStyleSheet("color: #c0392b;")
        self.error.setWordWrap(True)
        self.detail = QWidget()
        right = QVBoxLayout(self.detail)
        right.addLayout(form)
        right.addWidget(self.error)
        right.addLayout(buttons)
        right.addStretch(1)
        self.empty = QLabel("This keep watches no folders yet. Add one to begin.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QHBoxLayout(self)
        layout.addLayout(left, 2)
        layout.addWidget(self.detail, 5)
        layout.addWidget(self.empty, 5)
        self.reload()

    # --- showing ---

    def reload(self) -> None:
        """List the roots again and read their status in a worker."""
        current = self.current_root()
        self.roots.blockSignals(True)
        self.roots.clear()
        for root in self.session.keep.config.roots:
            item = QListWidgetItem(root.name)
            item.setData(_ROOT_ID, root.id)
            self.roots.addItem(item)
            if current is not None and root.id == current.id:
                self.roots.setCurrentItem(item)
        if self.roots.currentRow() < 0 and self.roots.count():
            self.roots.setCurrentRow(0)
        self.roots.blockSignals(False)
        self._show_root()
        session = self.session

        def job() -> dict[str, RootStatus]:
            with session.reader.connect() as conn:
                return root_statuses(conn)

        def done(statuses: dict[str, RootStatus]) -> None:
            if shiboken6.isValid(self):
                self.statuses = statuses
                self._show_status()

        run_in_pool(job, on_done=done, pool=self._pool)

    def current_root(self) -> RootConfig | None:
        if self.roots.currentRow() < 0:
            return None
        root_id = self.roots.currentItem().data(_ROOT_ID)
        return next((r for r in self.session.keep.config.roots if r.id == root_id), None)

    def select_root(self, root_id: str) -> None:
        for row in range(self.roots.count()):
            if self.roots.item(row).data(_ROOT_ID) == root_id:
                self.roots.setCurrentRow(row)

    def _show_root(self) -> None:
        root = self.current_root()
        self.detail.setVisible(root is not None)
        self.empty.setVisible(root is None)
        self.error.clear()
        if root is None:
            return
        self.name.setText(root.name)
        self.path.setText(root.path)
        override = self.session.settings.root_overrides.get(self.session.keep.config.id, {})
        self.local.setText(override.get(root.id, ""))
        self.exclude.setPlainText("\n".join(root.exclude))
        self.watch_button.setText("Stop watching" if root.watched else "Watch again")
        self.watch_button.setToolTip(
            "Stop scanning it; its items stay (shown offline) until you watch it again"
            if root.watched
            else "Scan it again; its items reconnect"
        )
        self.scan_button.setEnabled(root.watched)
        self._show_status()

    def _show_status(self) -> None:
        for row in range(self.roots.count()):
            item = self.roots.item(row)
            listed = next(r for r in self.session.keep.config.roots if r.id == item.data(_ROOT_ID))
            status = self.statuses.get(listed.id)
            item.setText(f"{listed.name}  —  {_state(listed, status)}")
        root = self.current_root()
        if root is not None:
            self.status.setText(describe_status(root, self.statuses.get(root.id)))

    # --- editing (each change saved at once, in a worker) ---

    def _edit(self, **changes: object) -> None:
        root = self.current_root()
        if root is None:
            return
        if all(getattr(root, k) == v for k, v in changes.items()):
            return
        session = self.session
        try:
            config = edit_root(session.keep.config, session.keep.dir, root.id, **changes)  # type: ignore[arg-type]
        except KeepError as e:
            self._refused(str(e))
            return
        offers_scan = "path" in changes or "exclude" in changes
        message = f"Saved {root.name}." + (" Scan it to update its files." if offers_scan else "")
        self._run(lambda: session.save_config(config), message)

    def _edit_path(self, path: str) -> None:
        self._edit(path=path)

    def _set_local(self, path: str) -> None:
        root = self.current_root()
        if root is None:
            return
        settings, keep_id = self.session.settings, self.session.keep.config.id
        current = settings.root_overrides.get(keep_id, {}).get(root.id, "")
        if path.strip() == current:
            return
        settings.set_root_override(keep_id, root.id, path.strip() or None)
        settings_path = self.settings_path
        message = (
            f"This computer now reaches {root.name} at {path.strip()}."
            if path.strip()
            else f"This computer uses {root.name}'s folder again."
        )
        self._run(lambda: save_settings(settings, settings_path), message)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Save the exclude patterns when the box loses the focus."""
        if watched is self.exclude and event.type() == QEvent.Type.FocusOut:
            self._edit(exclude=self.exclude.toPlainText().splitlines())
        return super().eventFilter(watched, event)

    def _choose_new_root(self) -> None:
        folder = self.pick_folder("Watch a folder")
        if folder:
            self.add_folder(folder)

    def add_folder(self, folder: str) -> None:
        """Watch ``folder`` (or watch again the root that watched it), then offer a scan."""
        session = self.session
        try:
            config = add_root(session.keep.config, session.keep.dir, "", folder)
        except KeepError as e:
            self._refused(str(e))
            return
        known = {r.id for r in session.keep.config.roots}
        root = next(
            (r for r in config.roots if r.id not in known),
            next(r for r in config.roots if r.path.strip() and _same(r.path, folder)),
        )

        def saved() -> None:
            self.select_root(root.id)
            if self.ask_scan(root):
                self.scan_requested.emit([root.id])

        self._run(lambda: session.save_config(config), f"Now watching {root.name}.", saved)

    def _toggle_watched(self) -> None:
        root = self.current_root()
        if root is None:
            return
        session, root_id = self.session, root.id
        if root.watched:
            self._run(
                lambda: session.stop_watching(root_id),
                f"Stopped watching {root.name}; its items stay, shown offline.",
            )
        else:

            def watched() -> None:
                if self.ask_scan(root):
                    self.scan_requested.emit([root_id])

            self._run(lambda: session.watch_again(root_id), f"Watching {root.name} again.", watched)

    def _scan(self) -> None:
        root = self.current_root()
        if root is not None:
            self.scan_requested.emit([root.id])

    def _remove(self) -> None:
        root = self.current_root()
        if root is None:
            return
        session, root_id = self.session, root.id

        def count() -> RemovalCounts:
            return session.writer.run(lambda conn: removal_counts(conn, root_id))

        def counted(counts: RemovalCounts) -> None:
            if not shiboken6.isValid(self):
                return
            choice = self.ask_remove(root, counts)
            if choice == KEEP:
                self._run(
                    lambda: session.stop_watching(root_id),
                    f"Stopped watching {root.name}; its items stay, shown offline.",
                )
            elif choice == DELETE and self.confirm_delete(root, counts):
                self._run(
                    lambda: session.delete_root(root_id),
                    f"Removed {root.name} and {_items(counts.deleted)} from the keep.",
                    lambda: save_settings(session.settings, self.settings_path),
                )

        run_in_pool(count, on_done=counted, pool=self._pool)

    def _run(
        self, work: Callable[[], object], message: str, then: Callable[[], None] | None = None
    ) -> None:
        self.busy += 1
        self.error.clear()

        def done(_: object) -> None:
            if not shiboken6.isValid(self):
                return
            self.busy -= 1
            self.reload()
            self.changed.emit(message)
            if then is not None:
                then()

        def failed(error: BaseException) -> None:
            if not shiboken6.isValid(self):
                return
            self.busy -= 1
            logger.error("Saving the keep's configuration failed", exc_info=error)
            self._refused(str(error))

        run_in_pool(work, on_done=done, on_error=failed, pool=self._pool)

    def _refused(self, message: str) -> None:
        self.error.setText(message)
        self._show_root()
        self.error.setText(message)

    def _browse(self, use: Callable[[str], None]) -> None:
        folder = self.pick_folder("Choose the folder")
        if folder:
            use(folder)

    # --- asking (tests replace these) ---

    def pick_folder(self, title: str) -> str:
        return QFileDialog.getExistingDirectory(self, title)

    def ask_scan(self, root: RootConfig) -> bool:
        answer = QMessageBox.question(
            self,
            "Scan now?",
            f"Scan {root.name} now, to find its files?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return answer == QMessageBox.StandardButton.Yes

    def ask_remove(self, root: RootConfig, counts: RemovalCounts) -> str | None:
        """``KEEP``, ``DELETE``, or ``None`` (cancelled)."""
        dialog = RemoveRootDialog(root, counts, self)
        return dialog.choice() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def confirm_delete(self, root: RootConfig, counts: RemovalCounts) -> bool:
        answer = QMessageBox.warning(
            self,
            "Delete items",
            f"Delete {_items(counts.deleted)} and their tags from this keep? This can't be "
            "undone.\n\nNo files on disk are changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes


class RemoveRootDialog(QDialog):
    """Remove a root: stop watching it (the default, keeping its items) or delete them."""

    def __init__(self, root: RootConfig, counts: RemovalCounts, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Remove {root.name}")
        self.keep = QRadioButton("Stop watching it, and keep its items")
        self.keep.setChecked(True)
        keep_text = QLabel(
            "Its items stay, with their tags, shown as offline. Add the folder again (or "
            "Watch again) and they reconnect."
        )
        self.delete = QRadioButton("Delete its items from this keep")
        kept = f" {_items(counts.kept)} keep files in other folders." if counts.kept else ""
        delete_text = QLabel(f"{_items(counts.deleted).capitalize()} and their tags go.{kept}")
        group = QButtonGroup(self)
        group.addButton(self.keep)
        group.addButton(self.delete)
        for text in (keep_text, delete_text):
            text.setWordWrap(True)
            text.setContentsMargins(24, 0, 0, 8)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Remove")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>Remove {html.escape(root.name)}?</b>"))
        layout.addWidget(self.keep)
        layout.addWidget(keep_text)
        layout.addWidget(self.delete)
        layout.addWidget(delete_text)
        layout.addWidget(QLabel("No files on disk are ever changed."))
        layout.addWidget(buttons)

    def choice(self) -> str:
        return DELETE if self.delete.isChecked() else KEEP


# --- text ---


def _items(count: int) -> str:
    return "1 item" if count == 1 else f"{count:,} items"


def _same(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


def _state(root: RootConfig, status: RootStatus | None) -> str:
    if not root.watched:
        return "not watched"
    if status is None or status.last_scan_at is None:
        return "not scanned yet"
    return "online" if status.online else "offline"


def describe_status(root: RootConfig, status: RootStatus | None) -> str:
    """The status line for one root (rich text)."""
    if status is None:
        return "Not scanned yet."
    parts = [html.escape(_state(root, status).capitalize())]
    files = f"{status.files:,} files and folders"
    details = [
        f"{n:,} {what}"
        for n, what in ((status.offline, "offline"), (status.missing, "missing"))
        if n
    ]
    if details:
        files += f" ({', '.join(details)})"
    parts.append(files)
    if status.last_scan_at is not None:
        parts.append(f"last scanned {status.last_scan_at.astimezone():%Y-%m-%d %H:%M}")
    text = " · ".join(parts)
    if status.last_error and root.watched:
        text += f'<br><span style="color:#c0392b">{html.escape(status.last_error)}</span>'
    return text
