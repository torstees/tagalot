"""The Keep configuration window (DESIGN.md §12 "Keep configuration").

A window of its own (Keep → Configure keep…), one per main window, with three tabs:

- **Folders:** the roots on the left; the selected one's name, folder, this computer's
  folder, exclude patterns, theme option overrides, and status on the right, with Scan
  now, Stop watching / Watch again, and Remove….
- **Thumbnails:** the largest size thumbnails are made at, and the stored thumbnails, with
  Clear….
- **Keep:** the keep's name and folder, its theme, and the theme's options.

Every change is checked and saved as soon as it's made (``keep.toml``, or ``settings.toml``
for this computer's folder), in a worker; a bad value is explained under the form and the
field shows the saved value again. A changed folder or exclude list offers a scan.
"""

import html
import logging
from collections.abc import Callable
from pathlib import Path

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.formats import format_bytes
from tagalot.core.keep import KeepError, RootConfig
from tagalot.core.keep_settings import MIN_THUMBNAIL_SIZE, with_name, with_option
from tagalot.core.online import consent_text
from tagalot.core.root_admin import (
    RemovalCounts,
    RootStatus,
    add_root,
    edit_root,
    parse_skip_lines,
    removal_counts,
    root_statuses,
    skip_lines,
)
from tagalot.core.session import KeepSession
from tagalot.core.settings import save_settings
from tagalot.core.thumbnails.cache import CacheStats
from tagalot.themes.api import ThemeOption, entity_plural
from tagalot.themes.loader import MAX_THUMBNAIL_SIZE
from tagalot.ui.folder_picker import choose_folder
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

KEEP, DELETE = "keep", "delete"
"""The two ways to remove a root."""

THUMBNAILS_TAB = 1
STATS_INTERVAL_MS = 3000
"""How often the stored-thumbnail count is read while the Thumbnails tab is in view."""

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
    thumbnail_max_changed = Signal()
    """The largest thumbnail size changed (the main window's size menu follows)."""
    clear_thumbnails_requested = Signal()
    """Clear the thumbnail cache (the main window asks and does it)."""

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
        self._scans_waiting: list[str] = []
        """Roots whose Scan now waits for a save in progress (the edit that clicking the
        button just finished), so the scan uses the saved settings."""
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
        self.exclude.setPlaceholderText(
            "One pattern per line, like **/cache/**  # an optional note saying why"
        )
        self.exclude.setToolTip(
            "Files and folders matching these patterns are skipped when scanning. After a "
            "pattern, ' # ' starts a note saying why it is there (Tagalot writes one when it "
            "skips a file itself). Saved when you leave the box."
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
        self.writable = QCheckBox("Let Write to file… change files in this folder")
        self.writable.setObjectName("writable")
        self.writable.setToolTip(
            "Off: Tagalot never changes a file here. On: Write to file… (and only that) may "
            "update the metadata of files here, after showing you the change, keeping a copy "
            "in the keep's backups folder."
        )
        self.writable.clicked.connect(self._toggle_writable)
        form.addRow("Write back:", self.writable)
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
        self.root_options = QGroupBox("Theme options for this folder")
        self.root_options.setToolTip(
            "Settings of the theme for this folder only; unticked ones use the keep's "
            "(Keep tab). They apply at the next scan."
        )
        self.root_options_form = QFormLayout(self.root_options)
        self.root_options.setVisible(bool(session.theme.options))
        right.addWidget(self.root_options)
        right.addWidget(self.error)
        right.addLayout(buttons)
        right.addStretch(1)
        self.empty = QLabel("This keep watches no folders yet. Add one to begin.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        folders = QWidget()
        folders_layout = QHBoxLayout(folders)
        folders_layout.addLayout(left, 2)
        folders_layout.addWidget(self.detail, 5)
        folders_layout.addWidget(self.empty, 5)
        self.tabs = QTabWidget()
        self.tabs.addTab(folders, "Folders")
        self.tabs.addTab(self._thumbnails_tab(), "Thumbnails")
        self.tabs.addTab(self._keep_tab(), "Keep")
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        # Thumbnails are stored as the grids show them: count them again while the
        # Thumbnails tab is in view, and whenever it comes into view.
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(STATS_INTERVAL_MS)
        self._stats_timer.timeout.connect(self.show_cache_stats)
        self.tabs.currentChanged.connect(lambda _: self._watch_stats())
        self.reload()

    # --- tabs ---

    def _thumbnails_tab(self) -> QWidget:
        theme_size = self.session.theme.thumbnail_max
        self.theme_size = QCheckBox(f"The theme's size ({theme_size} px)")
        self.theme_size.setToolTip("Use the size the theme suggests for its pictures")
        self.max_size = QSpinBox()
        self.max_size.setRange(MIN_THUMBNAIL_SIZE, MAX_THUMBNAIL_SIZE)
        self.max_size.setSingleStep(64)
        self.max_size.setSuffix(" px")
        self.max_size.setToolTip(
            "Thumbnails are made (and stored) at this size; grids can show them smaller. "
            "Bigger looks sharper when zoomed in, and takes more space."
        )
        self.theme_size.toggled.connect(self._theme_size_toggled)
        self.max_size.editingFinished.connect(
            lambda: self._set_thumbnail_max(self.max_size.value())
        )
        size_row = QHBoxLayout()
        size_row.addWidget(self.theme_size)
        size_row.addWidget(self.max_size)
        size_row.addStretch(1)
        self.cache_stats = QLabel()
        self.cache_stats.setObjectName("cache_stats")
        clear = QPushButton("Clear\u2026")
        clear.setToolTip("Delete every stored thumbnail; they are made again as you browse")
        clear.clicked.connect(self.clear_thumbnails_requested)
        cache_row = QHBoxLayout()
        cache_row.addWidget(self.cache_stats, 1)
        cache_row.addWidget(clear)
        self.after_scan = QCheckBox("Make new and changed items' thumbnails after each scan")
        self.after_scan.setToolTip(
            "In the background, so they're ready when you browse. Turn it off for a huge "
            "keep on a slow share: thumbnails are then made only as pages show them."
        )
        self.after_scan.toggled.connect(self._set_after_scan)
        form = QFormLayout()
        form.addRow("Largest size:", size_row)
        form.addRow("", self.after_scan)
        form.addRow("Stored:", cache_row)
        tab = QWidget()
        column = QVBoxLayout(tab)
        column.addLayout(form)
        column.addStretch(1)
        return tab

    def _keep_tab(self) -> QWidget:
        session = self.session
        self.keep_name = QLineEdit()
        self.keep_name.editingFinished.connect(lambda: self._rename_keep(self.keep_name.text()))
        folder = QLabel(str(session.keep.dir))
        folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        theme = session.theme
        loaded = session.catalog.get(theme.id)
        where = "built in" if loaded is None or loaded.builtin else loaded.source
        theme_label = QLabel(
            f"{html.escape(theme.name)} (<code>{theme.id}</code>, version {theme.version}, "
            f"{html.escape(where)})"
        )
        theme_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        types = QLabel(", ".join(entity_plural(e) for e in theme.entities))
        about = QLabel(_first_paragraph(theme.__doc__))
        about.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Name:", self.keep_name)
        form.addRow("Folder:", folder)
        form.addRow("Theme:", theme_label)
        form.addRow("Holds:", types)
        if about.text():
            form.addRow("", about)
        self.keep_options = QGroupBox("Theme options")
        self.keep_options.setToolTip(
            "The theme's settings for this keep; a folder can override them (Folders tab). "
            "They apply at the next scan, which reads the affected files again."
        )
        self.keep_options_form = QFormLayout(self.keep_options)
        self.keep_options.setVisible(bool(theme.options))
        self.online = QGroupBox("Online details")
        self.online_lookups = QCheckBox("Look up details online")
        self.online_lookups.toggled.connect(self._set_online_lookups)
        sends = QLabel(consent_text(theme.online_sources))
        sends.setWordWrap(True)
        online_column = QVBoxLayout(self.online)
        online_column.addWidget(self.online_lookups)
        online_column.addWidget(sends)
        self.online.setVisible(bool(theme.online_sources))
        tab = QWidget()
        column = QVBoxLayout(tab)
        column.addLayout(form)
        column.addWidget(self.keep_options)
        column.addWidget(self.online)
        column.addStretch(1)
        return tab

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
        self._show_settings()
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
        self.exclude.setPlainText("\n".join(skip_lines(root)))
        self.watch_button.setText("Stop watching" if root.watched else "Watch again")
        self.watch_button.setToolTip(
            "Stop scanning it; its items stay (shown offline) until you watch it again"
            if root.watched
            else "Scan it again; its items reconnect"
        )
        self.scan_button.setEnabled(root.watched)
        self.writable.setChecked(root.writable)
        self._show_root_options(root)
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

    def _show_settings(self) -> None:
        """The Thumbnails and Keep tabs, from the saved configuration."""
        config = self.session.keep.config
        self.keep_name.setText(config.name)
        self.setWindowTitle(f"Configure {config.name}")
        self.theme_size.blockSignals(True)
        self.theme_size.setChecked(config.thumbnail_max is None)
        self.theme_size.blockSignals(False)
        self.max_size.setValue(self.session.thumbnail_max)
        self.after_scan.blockSignals(True)
        self.after_scan.setChecked(config.thumbnails_after_scan)
        self.after_scan.blockSignals(False)
        self.max_size.setEnabled(config.thumbnail_max is not None)
        self.online_lookups.blockSignals(True)
        self.online_lookups.setChecked(config.online_lookups == "allow")
        self.online_lookups.blockSignals(False)
        _clear_form(self.keep_options_form)
        for spec in self.session.theme.options:
            value = config.theme_options.get(spec.name, spec.default)
            editor = OptionEditor(spec, value)
            editor.committed.connect(lambda v, name=spec.name: self._set_option(name, v))
            reset = QPushButton("Default")
            reset.setToolTip(f"Use the theme's default: {_shown(spec.default)}")
            reset.setEnabled(spec.name in config.theme_options)
            reset.clicked.connect(lambda _=False, name=spec.name: self._set_option(name, None))
            row = QHBoxLayout()
            row.addWidget(editor, 1)
            row.addWidget(reset)
            label = QLabel(f"{spec.label}:")
            label.setToolTip(spec.description)
            self.keep_options_form.addRow(label, row)
        self.show_cache_stats()

    def _show_root_options(self, root: RootConfig) -> None:
        _clear_form(self.root_options_form)
        keep_values = self.session.keep.config.theme_options
        for spec in self.session.theme.options:
            inherited = keep_values.get(spec.name, spec.default)
            overridden = spec.name in root.options
            use_own = QCheckBox(spec.label)
            use_own.setToolTip(
                f"{spec.description}\n\nUnticked: the keep's setting ({_shown(inherited)})."
            )
            use_own.setChecked(overridden)
            editor = OptionEditor(spec, root.options.get(spec.name, inherited))
            editor.setEnabled(overridden)
            editor.committed.connect(
                lambda v, name=spec.name, rid=root.id: self._set_option(name, v, rid)
            )
            use_own.toggled.connect(
                lambda on, name=spec.name, rid=root.id, e=editor: self._set_option(
                    name, e.value() if on else None, rid
                )
            )
            self.root_options_form.addRow(use_own, editor)

    def _thumbnails_shown(self) -> bool:
        return self.isVisible() and self.tabs.currentIndex() == THUMBNAILS_TAB

    def _watch_stats(self) -> None:
        """Count now if the Thumbnails tab is in view, and keep counting while it is."""
        if self._thumbnails_shown():
            self.show_cache_stats()
            self._stats_timer.start()
        else:
            self._stats_timer.stop()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._watch_stats()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._stats_timer.stop()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self._watch_stats()

    def show_cache_stats(self) -> None:
        """How many thumbnails are stored, read in a worker."""
        cache = self.session.thumbnails.cache

        def done(stats: CacheStats) -> None:
            if shiboken6.isValid(self):
                self.cache_stats.setText(
                    f"{stats.count:,} thumbnails ({format_bytes(stats.bytes)})"
                    if stats.count
                    else "None yet"
                )

        run_in_pool(cache.stats, on_done=done, pool=self._pool)

    # --- editing (each change saved at once, in a worker) ---

    def _rename_keep(self, name: str) -> None:
        if name.strip() == self.session.keep.config.name:
            return
        try:
            with_name(self.session.keep.config, self.session.keep.dir, name)
        except KeepError as e:
            self._refused(str(e))
            return
        session = self.session
        self._run(lambda: session.rename_keep(name), f"Renamed the keep {name.strip()}.")

    def _theme_size_toggled(self, use_theme: bool) -> None:
        self.max_size.setEnabled(not use_theme)
        self._set_thumbnail_max(None if use_theme else self.max_size.value())

    def _set_thumbnail_max(self, size: int | None) -> None:
        if size == self.session.keep.config.thumbnail_max:
            return
        session = self.session
        shown = size or session.theme.thumbnail_max
        self._run(
            lambda: session.set_thumbnail_max(size),
            f"Thumbnails are now made at {shown} px; they're made again as they're shown.",
            self.thumbnail_max_changed.emit,
        )

    def _set_after_scan(self, on: bool) -> None:
        session = self.session
        self._run(
            lambda: session.set_thumbnails_after_scan(on),
            "Scans now make thumbnails in the background."
            if on
            else "Thumbnails are now made only as pages show them.",
        )

    def _set_online_lookups(self, on: bool) -> None:
        session = self.session
        self._run(
            lambda: session.set_online_lookups("allow" if on else "never"),
            "Details are now looked up online." if on else "Nothing is looked up online now.",
        )

    def _set_option(self, name: str, value: object, root_id: str | None = None) -> None:
        session = self.session
        try:
            with_option(session.keep.config, session.theme, name, value, root_id=root_id)
        except KeepError as e:
            self._refused(str(e))
            return
        self._run(
            lambda: session.set_option(name, value, root_id),
            "Saved the theme option; it applies at the next scan (F5).",
        )

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

    def _toggle_writable(self, on: bool) -> None:
        """Mark the folder writable (after asking) or not."""
        root = self.current_root()
        if root is None:
            return
        if on:
            answer = QMessageBox.question(
                self,
                "Let Tagalot write to this folder?",
                f"Let Write to file… change files in {root.name} ({root.path})?\n\n"
                "Tagalot still changes nothing on its own: only when you choose Write to "
                "file… on items, after showing you each change. It changes only the "
                "metadata block (a Markdown file's front matter), refuses a file that "
                "changed since it was read, and keeps a copy of each file it changes in the "
                "keep's backups folder.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.writable.setChecked(False)
                return
        self._edit(writable=on)

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
            patterns, notes = parse_skip_lines(self.exclude.toPlainText().splitlines())
            self._edit(exclude=patterns, exclude_notes=notes)
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
        if root is None:
            return
        if self.busy:  # clicking the button ended an edit that is still being saved
            if root.id not in self._scans_waiting:
                self._scans_waiting.append(root.id)
            return
        self.scan_requested.emit([root.id])

    def _scan_when_saved(self) -> None:
        if not self.busy and self._scans_waiting:
            waiting, self._scans_waiting = self._scans_waiting, []
            self.scan_requested.emit(waiting)

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
            self._scan_when_saved()

        def failed(error: BaseException) -> None:
            if not shiboken6.isValid(self):
                return
            self.busy -= 1
            logger.error("Saving the keep's configuration failed", exc_info=error)
            self._refused(str(error))
            self._scan_when_saved()

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
        return choose_folder(self, title, self.session.settings, self.settings_path)

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
        for n, what in (
            (status.offline, "offline"),
            (status.missing, "missing"),
            (status.skipped, "skipped"),
        )
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


class OptionEditor(QWidget):
    """An editor for one theme option, by its type: a checkbox, a number box, or a text box.
    Emits :attr:`committed` with the new value when the edit is done."""

    committed = Signal(object)

    def __init__(self, spec: ThemeOption, value: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setToolTip(spec.description)
        self.widget: QCheckBox | QSpinBox | QDoubleSpinBox | QLineEdit
        if spec.type is bool:
            box = QCheckBox()
            box.setChecked(bool(value))
            box.toggled.connect(lambda on: self.committed.emit(on))
            self.widget = box
        elif spec.type is int:
            spin = QSpinBox()
            spin.setRange(-1_000_000, 1_000_000)
            spin.setValue(int(value) if isinstance(value, int | float) else 0)
            spin.editingFinished.connect(lambda: self.committed.emit(spin.value()))
            self.widget = spin
        elif spec.type is float:
            number = QDoubleSpinBox()
            number.setRange(-1e9, 1e9)
            number.setDecimals(3)
            number.setValue(float(value) if isinstance(value, int | float) else 0.0)
            number.editingFinished.connect(lambda: self.committed.emit(number.value()))
            self.widget = number
        else:
            text = QLineEdit(str(value))
            text.editingFinished.connect(lambda: self.committed.emit(text.text()))
            self.widget = text
        self.widget.setObjectName(f"option_{spec.name}")
        layout.addWidget(self.widget)

    def value(self) -> object:
        widget = self.widget
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QSpinBox | QDoubleSpinBox):
            return widget.value()
        return widget.text()


def _clear_form(form: QFormLayout) -> None:
    while form.rowCount():
        form.removeRow(0)


def _shown(value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def _first_paragraph(doc: str | None) -> str:
    if not doc:
        return ""
    return " ".join(doc.strip().split("\n\n")[0].split())
