"""Keep launcher: recent keeps, create, and open (DESIGN.md §12).

A separate start dialog. Everything that touches the disk (reading recent keeps' ``keep.toml``,
loading themes, creating and opening keeps) runs in a worker.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.keep import (
    KeepConfig,
    KeepError,
    RootConfig,
    ThemeRef,
    create_keep,
    load_keep_config,
)
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings, save_settings
from tagalot.themes.loader import ThemeCatalog, load_themes
from tagalot.ui.opening import open_keep_async
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

_PATH = Qt.ItemDataRole.UserRole + 1


@dataclass(frozen=True)
class RecentKeep:
    """A recent keep as the launcher shows it."""

    path: Path
    config: KeepConfig | None
    """``None`` when the keep can't be read (moved, deleted, drive not connected)."""
    problem: str | None = None


def describe_recent(paths: list[Path]) -> list[RecentKeep]:
    """Read each recent keep's ``keep.toml`` (in a worker: keeps may be on slow drives)."""
    result = []
    for path in paths:
        try:
            result.append(RecentKeep(path, load_keep_config(path / "keep.toml")))
        except KeepError as e:
            result.append(RecentKeep(path, None, str(e)))
    return result


def folder_name(folder: str) -> str:
    r"""The last segment of a folder as typed, with either slash style.

    ``Path`` can't be used: for a share root like ``\\nas\music`` its ``name`` is empty
    (Windows treats the share as a drive root).
    """
    parts = [p for p in re.split(r"[\\/]+", folder.strip()) if p]
    return parts[-1] if parts else folder.strip()


def root_id_for(folder: str) -> str:
    """A stable, readable root id from a folder: ``"D:/My Photos"`` -> ``"my-photos"``."""
    slug = re.sub(r"[^a-z0-9]+", "-", folder_name(folder).lower()).strip("-")
    return slug or "root"


class LauncherDialog(QDialog):
    """Pick a recent keep, open a keep folder, or create a new keep.

    Emits :attr:`opened` with the :class:`KeepSession` once a keep is open.
    """

    opened = Signal(object)

    def __init__(
        self,
        settings: Settings,
        *,
        settings_path: Path | None = None,
        catalog: ThemeCatalog | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tagalot")
        self.resize(560, 360)
        self.settings = settings
        self.settings_path = settings_path
        self.catalog = catalog

        self.recent = QListWidget()
        self.recent.itemDoubleClicked.connect(lambda item: self._open(item.data(_PATH)))
        self.recent.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.recent.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.recent.customContextMenuRequested.connect(self._recent_menu)

        self.new_button = QPushButton("New keep…")
        self.new_button.clicked.connect(self._new_keep)
        self.folder_button = QPushButton("Open folder…")
        self.folder_button.clicked.connect(self._open_folder)
        self.open_button = QPushButton("Open")
        self.open_button.setDefault(True)
        self.open_button.clicked.connect(self._open_selected)
        buttons = QHBoxLayout()
        buttons.addWidget(self.new_button)
        buttons.addWidget(self.folder_button)
        buttons.addStretch(1)
        buttons.addWidget(self.open_button)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.problems = QLabel()
        self.problems.setTextFormat(Qt.TextFormat.RichText)
        self.problems.linkActivated.connect(lambda _: self._show_problems())
        self.problems.setVisible(False)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Recent keeps"))
        layout.addWidget(self.recent)
        layout.addLayout(buttons)
        layout.addWidget(self.status)
        layout.addWidget(self.problems)

        self._busy = False
        self._update_buttons()
        self._load_recent()
        if self.catalog is None:
            run_in_pool(
                lambda: load_themes(extra_dirs=settings.theme_dirs), on_done=self._catalog_loaded
            )
        else:
            self._catalog_loaded(self.catalog)

    # --- loading ---

    def _load_recent(self) -> None:
        paths = list(self.settings.recent_keeps)
        run_in_pool(lambda: describe_recent(paths), on_done=self._show_recent)

    def _show_recent(self, recent: list[RecentKeep]) -> None:
        self.recent.clear()
        for keep in recent:
            if keep.config is not None:
                theme = self._theme_name(keep.config.theme.id)
                text = f"{keep.config.name}    {theme} · {keep.path}"
            else:
                text = f"{keep.path.name}    not found · {keep.path}"
            item = QListWidgetItem(text)
            item.setData(_PATH, keep.path)
            item.setToolTip(keep.problem or str(keep.path))
            if keep.config is None:
                item.setForeground(self.palette().placeholderText())
            self.recent.addItem(item)
        if self.recent.count():
            self.recent.setCurrentRow(0)
        else:
            self.status.setText("No recent keeps yet. Create one, or open a keep folder.")
        self._update_buttons()

    def _catalog_loaded(self, catalog: ThemeCatalog) -> None:
        self.catalog = catalog
        count = len(catalog.problems)
        if count:
            files = "file has" if count == 1 else "files have"
            self.problems.setText(f"⚠ {count} theme {files} problems (<a href='#'>details</a>)")
            self.problems.setVisible(True)
        self._update_buttons()

    def _theme_name(self, theme_id: str) -> str:
        loaded = self.catalog.get(theme_id) if self.catalog else None
        return loaded.theme.name if loaded else theme_id

    # --- actions ---

    def _open_selected(self) -> None:
        item = self.recent.currentItem()
        if item is not None:
            self._open(item.data(_PATH))

    def _open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Open a keep folder")
        if folder:
            self._open(Path(folder))

    def _open(self, keep_dir: Path) -> None:
        if self._busy:
            return
        self._set_busy(f"Opening {keep_dir.name}…")
        open_keep_async(
            self,
            keep_dir,
            self.settings,
            self._opened,
            settings_path=self.settings_path,
            on_failed=lambda _: self._set_idle(),
        )

    def _opened(self, session: KeepSession) -> None:
        self._set_idle()
        self.opened.emit(session)
        self.accept()

    def _new_keep(self) -> None:
        if self.catalog is None:
            return
        dialog = NewKeepDialog(self.catalog, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._create(dialog)

    def _create(self, dialog: "NewKeepDialog") -> None:
        keep_dir, name, theme, root = (
            dialog.keep_dir(),
            dialog.name(),
            dialog.theme(),
            dialog.root(),
        )
        self._set_busy(f"Creating {name}…")

        def work() -> Path:
            rid = root_id_for(root)
            create_keep(keep_dir, name, theme, [RootConfig(rid, folder_name(root) or rid, root)])
            return keep_dir

        def failed(error: BaseException) -> None:
            self._set_idle()
            QMessageBox.warning(self, "Can't create this keep", str(error))

        run_in_pool(work, on_done=self._open_created, on_error=failed)

    def _open_created(self, keep_dir: Path) -> None:
        self._busy = False
        self._open(keep_dir)

    def _recent_menu(self, position: QPoint) -> None:
        item = self.recent.itemAt(position)
        if item is None:
            return
        menu = QMenu(self)
        remove = menu.addAction("Remove from list")
        if menu.exec(self.recent.viewport().mapToGlobal(position)) == remove:
            self.remove_recent(item.data(_PATH))

    def remove_recent(self, keep_dir: Path) -> None:
        """Forget a keep in the recent list (the keep itself is untouched)."""
        self.settings.remove_recent_keep(keep_dir)
        settings, path = self.settings, self.settings_path
        run_in_pool(lambda: save_settings(settings, path))
        for row in range(self.recent.count()):
            if self.recent.item(row).data(_PATH) == keep_dir:
                self.recent.takeItem(row)
                break
        self._update_buttons()

    def _show_problems(self) -> None:
        if self.catalog is None:
            return
        details = "\n".join(f"{p.source}\n    {p.message}" for p in self.catalog.problems)
        QMessageBox.information(self, "Theme problems", details)

    # --- state ---

    def _set_busy(self, message: str) -> None:
        self._busy = True
        self.status.setText(message)
        self._update_buttons()

    def _set_idle(self) -> None:
        self._busy = False
        self.status.setText("")
        self._update_buttons()

    def _update_buttons(self) -> None:
        idle = not self._busy
        self.open_button.setEnabled(idle and self.recent.currentItem() is not None)
        self.folder_button.setEnabled(idle)
        self.new_button.setEnabled(idle and self.catalog is not None and bool(self.catalog.themes))
        self.recent.setEnabled(idle)


class NewKeepDialog(QDialog):
    """Name, location, theme, and first root for a new keep (DESIGN.md §12)."""

    def __init__(self, catalog: ThemeCatalog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New keep")
        self.resize(520, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Music")
        self.location_edit = QLineEdit(str(Path.home()))
        self.root_edit = QLineEdit()
        self.root_edit.setPlaceholderText(
            "The folder this keep watches (a local or network folder)"
        )
        self.theme_combo = QComboBox()
        for loaded in sorted(catalog.themes.values(), key=lambda t: t.theme.name.lower()):
            self.theme_combo.addItem(
                loaded.theme.name, ThemeRef(loaded.theme.id, loaded.theme.version)
            )
        self.preview = QLabel()
        self.preview.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Location", self._with_browse(self.location_edit, "Where to put the keep"))
        form.addRow("Theme", self.theme_combo)
        form.addRow("Folder to watch", self._with_browse(self.root_edit, "Folder to watch"))
        form.addRow("", self.preview)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.buttons)

        for edit in (self.name_edit, self.location_edit, self.root_edit):
            edit.textChanged.connect(self._validate)
        self._validate()

    def name(self) -> str:
        return " ".join(self.name_edit.text().split())

    def keep_dir(self) -> Path:
        return Path(self.location_edit.text().strip()) / f"{self.name()}.keep"

    def theme(self) -> ThemeRef:
        ref = self.theme_combo.currentData()
        assert isinstance(ref, ThemeRef)
        return ref

    def root(self) -> str:
        """The folder to watch, exactly as typed (a UNC share keeps its form)."""
        return self.root_edit.text().strip()

    def problem(self) -> str | None:
        """Why the form can't be submitted yet, or ``None``."""
        if not self.name():
            return "Give the keep a name."
        if any(c in self.name() for c in '<>:"/\\|?*'):
            return "A keep name can't contain < > : \" / \\ | ? *"
        if not self.location_edit.text().strip():
            return "Choose where to put the keep."
        if not self.root_edit.text().strip():
            return "Choose a folder for the keep to watch."
        if self.theme_combo.count() == 0:
            return "No themes are available."
        return None

    def _validate(self) -> None:
        problem = self.problem()
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(problem is None)
        self.preview.setText(problem or f"The keep will be created in {self.keep_dir()}")

    def _with_browse(self, edit: QLineEdit, title: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit)
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: self._browse(edit, title))
        layout.addWidget(browse)
        return row

    def _browse(self, edit: QLineEdit, title: str) -> None:
        folder = QFileDialog.getExistingDirectory(self, title, edit.text())
        if folder:
            edit.setText(folder)
