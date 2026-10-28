"""The Keep configuration window (DESIGN.md §12 "Keep configuration").

A window of its own (Keep → Configure keep…), one per main window, with four tabs:

- **Folders:** the roots on the left; the selected one's name, folder, this computer's
  folder, exclude patterns, theme option overrides, and status on the right, with Scan
  now, Stop watching / Watch again, and Remove….
- **Thumbnails:** the largest size thumbnails are made at, and the stored thumbnails, with
  Clear….
- **Keep:** the keep's name and folder, its theme, and the theme's options.
- **Notes:** the keep's notes folder, Choose… and None (§9 *The notes folder*, #382); and
  the front-matter keys notes never make Extra fields: the keep's, the user's (every keep
  on this computer), and the built-in ones (§9 *Notes*, #381).

Every change is checked and saved as soon as it's made (``keep.toml``, or ``settings.toml``
for this computer's folder), in a worker; a bad value is explained under the form and the
field shows the saved value again. A changed folder or exclude list offers a scan.
"""

import html
import logging
from collections.abc import Callable, Mapping
from pathlib import Path

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QFont,
    QHideEvent,
    QIcon,
    QPalette,
    QPixmap,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
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
from sqlalchemy import select

from tagalot.core.color_schemes import (
    BUILT_IN,
    CUSTOM,
    NATIVE,
    USER,
    hard_to_read,
    name_problem,
    resolve_colors,
    scheme_colors,
)
from tagalot.core.contents import ContentsStats
from tagalot.core.extra_keys import DEFAULT_IGNORED_KEYS
from tagalot.core.fonts import SYSTEM_FONT, is_font_file
from tagalot.core.formats import format_bytes
from tagalot.core.google_fonts import HOST as GOOGLE_HOST
from tagalot.core.google_fonts import GoogleFont, google_fonts
from tagalot.core.keep import TEXT_SIZES, KeepError, RootConfig, clean_names
from tagalot.core.keep_settings import (
    MIN_THUMBNAIL_SIZE,
    with_colors,
    with_markdown_assets,
    with_name,
    with_option,
)
from tagalot.core.models import Entity
from tagalot.core.note_keys import NoteKeyError, alias_lines, check_key, parse_alias_lines
from tagalot.core.online import consent_text, needed_env, tokens_text
from tagalot.core.root_admin import (
    RemovalCounts,
    RootStatus,
    add_root,
    edit_root,
    notes_folder_for,
    parse_skip_lines,
    removal_counts,
    root_statuses,
    skip_lines,
    with_notes_folder,
)
from tagalot.core.roots import local_path
from tagalot.core.session import KeepSession
from tagalot.core.settings import ENV_NAME, save_settings
from tagalot.core.thumbnails.cache import CacheStats
from tagalot.themes.api import ColorScheme, ThemeOption, entity_plural
from tagalot.themes.loader import MAX_THUMBNAIL_SIZE
from tagalot.ui.appearance import apply_colors, starting_colors
from tagalot.ui.colors import ERROR, css
from tagalot.ui.folder_picker import choose_folder
from tagalot.ui.font_picker import FontChoice, FontPicker
from tagalot.ui.fonts import PlaceFont, apply_fonts, look_of
from tagalot.ui.icon_picker import IconChoice, IconPicker
from tagalot.ui.icons import icon
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

KEEP, DELETE = "keep", "delete"
"""The two ways to remove a root."""

THUMBNAILS_TAB = 1
STATS_INTERVAL_MS = 3000
"""How often the stored-thumbnail count is read while the Thumbnails tab is in view."""

_ROOT_ID = Qt.ItemDataRole.UserRole
ASSETS_TIP = (
    "Where Markdown files' pictures and links are looked for when they aren't beside the "
    "file (an attachments folder): a folder from the top of the watched folder, or "
    "from each file's own folder when it starts with ./ (./assets). A file's own assets "
    "front-matter key comes first."
)


COLOR_CHOICES = (
    ("Follow the system", "system"),
    ("Light", "light"),
    ("Dark", "dark"),
)
"""The system's looks at the top of the Colors list (#558): label, then ``[appearance]
colors``; color schemes follow (#572)."""
BUILT_IN_IDS = frozenset(s.id for s in BUILT_IN)
FONT_LABELS = {
    "titles": ("Titles", "Items' pages, grid cards, the navigation's headings, and title cards"),
    "notes": ("Notes", "Notes and glimpses of Markdown files on items' pages"),
    "text": ("Text", "The rest of the window"),
}
"""The Fonts group's places (#565): label, then tooltip."""


def theme_note(label: str, shown: PlaceFont) -> str:
    """What the Fonts group says of a theme's suggestions that aren't here."""
    names = shown.missing
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
    verb = "isn't" if len(names) == 1 else "aren't"
    instead = shown.family or "the system's font"
    return f"{label}: the theme's {listed} {verb} on this computer, so {instead} shows."


def download_offer(shown: PlaceFont) -> GoogleFont | None:
    """The first Google Font a theme suggested for a place that isn't here, to download."""
    known = {f.name.casefold(): f for f in google_fonts().families}
    for name in shown.missing:
        if name.casefold() in known:
            return known[name.casefold()]
    return None


def missing_text(value: str) -> str:
    """Why a keep's font isn't shown, for the Fonts group's note."""
    if is_font_file(value):
        return f"{value} isn't in the keep folder, or isn't a font Tagalot can read"
    return f"{value} isn't installed on this computer"


def sample_titles(session: KeepSession, count: int = 3) -> list[str]:
    """A few of the keep's items' titles, for previews of its fonts. Runs in a worker."""
    with session.reader.connect() as conn:
        titles = conn.scalars(
            select(Entity.title).where(Entity.title != "").order_by(Entity.id).limit(count)
        )
        return [session.keep.config.name, *titles]


COLOR_LABELS = {
    "background": "Background",
    "text": "Text",
    "panels": "Lists and panels",
    "accent": "Accent (selection, links)",
    "dimmed": "Dimmed text",
}


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
    online_allowed = Signal()
    contents_changed = Signal()
    """Contents search was turned on, off, or switched: the window builds the index and
    reads what it hasn't."""
    """Online lookups were just turned on: the window looks up what it hasn't yet."""
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
        self.archives = QPlainTextEdit()
        self.archives.setObjectName("archives")
        self.archives.setPlaceholderText(
            "One pattern per line, like Fonts/** or ** for every archive"
        )
        self.archives.setToolTip(
            "The files inside archives (zip, 7z, rar) in folders matching these patterns are "
            "read as files of their own, every one, those added later too. An archive's "
            "right-click menu chooses for that one archive. Saved when you leave the box."
        )
        self.archives.setMaximumHeight(72)
        self.archives.installEventFilter(self)
        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setTextFormat(Qt.TextFormat.RichText)
        self.status.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Name:", self.name)
        form.addRow("Folder:", path_row)
        form.addRow("On this computer:", local_row)
        form.addRow("Skip:", self.exclude)
        form.addRow("Read inside archives:", self.archives)
        self.writable = QCheckBox("Let Write to file… change files in this folder")
        self.writable.setObjectName("writable")
        self.writable.setToolTip(
            "Off: Tagalot never changes a file here. On: Write to file… (and only that) may "
            "update the metadata of files here, after showing you the change, keeping a copy "
            "in the keep's backups folder."
        )
        self.writable.clicked.connect(self._toggle_writable)
        form.addRow("Write back:", self.writable)
        self.assets = QLineEdit()
        self.assets.setObjectName("assets")
        self.assets.setToolTip(ASSETS_TIP + " This folder's own; empty uses the keep's (Keep tab).")
        self.assets.editingFinished.connect(lambda: self._edit(assets=self.assets.text()))
        form.addRow("Assets folder:", self.assets)
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
        self.error.setStyleSheet(f"color: {css(ERROR)};")
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
        self.tabs.addTab(self._notes_tab(), "Notes")
        self.tabs.addTab(self._appearance_tab(), "Appearance")
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
        self.keep_assets = QLineEdit()
        self.keep_assets.setObjectName("keep_assets")
        self.keep_assets.setPlaceholderText("None: only beside each file")
        self.keep_assets.setToolTip(
            ASSETS_TIP + " For every watched folder that doesn't name its own (Folders tab)."
        )
        self.keep_assets.editingFinished.connect(
            lambda: self._set_markdown_assets(self.keep_assets.text())
        )
        form.addRow("Assets folder:", self.keep_assets)
        self.see_through = QPlainTextEdit()
        self.see_through.setObjectName("see_through")
        self.see_through.setPlaceholderText("! Posters")
        self.see_through.setMaximumHeight(80)
        self.see_through.setToolTip(
            "Folders with these names are never items: the files in them count as their "
            "parent folder's (a poster in Heat (1995)/! Posters is Heat's). One name per line, "
            "matched exactly, case aside. Saved when you leave the box; the next scan (F5) "
            "reads the keep's files again."
        )
        self.see_through.installEventFilter(self)
        form.addRow("See-through folders:", self.see_through)
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
        self.online_sends = QLabel()
        self.online_sends.setWordWrap(True)
        self.find_by = QComboBox()
        for choice, find_by in FIND_BY_CHOICES:
            self.find_by.addItem(choice, find_by)
        self.find_by.setToolTip(
            "What may identify an item to a service. Titles find items that have no ID, "
            "such as a home video, but send their titles."
        )
        self.find_by.currentIndexChanged.connect(self._set_find_by)
        find_by_row = QHBoxLayout()
        find_by_row.addWidget(QLabel("Find items by:"))
        find_by_row.addWidget(self.find_by)
        find_by_row.addStretch()
        self.find_by_row = QWidget()
        self.find_by_row.setLayout(find_by_row)
        find_by_row.setContentsMargins(0, 0, 0, 0)
        self.find_by_row.setVisible(any(s.titles for s in theme.online_sources))
        self.tokens = QLabel(tokens_text(theme.online_sources, session.environment()))
        self.tokens.setWordWrap(True)
        self.tokens.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tokens.setVisible(bool(self.tokens.text()))
        online_column = QVBoxLayout(self.online)
        online_column.addWidget(self.online_lookups)
        online_column.addWidget(self.find_by_row)
        online_column.addWidget(self.online_sends)
        online_column.addWidget(self.tokens)
        self.online.setVisible(bool(theme.online_sources))
        self.contents = QGroupBox("Search inside documents")
        self.contents_index = QComboBox()
        for label, value in CONTENTS_CHOICES:
            self.contents_index.addItem(label, value)
        self.contents_index.activated.connect(
            lambda _: self._set_contents_index(self.contents_index.currentData())
        )
        about_index = QLabel(
            "Words finds whole words and the starts of words; Substrings also finds text "
            "inside words, and takes 3 to 5 times the space. Switching rebuilds the index "
            "from the text already read."
        )
        about_index.setWordWrap(True)
        self.contents_stats = QLabel()
        self.contents_stats.setObjectName("contents_stats")
        self.clear_contents = QPushButton("Clear contents text\u2026")
        self.clear_contents.setToolTip("Turn this off and delete the documents' text and the index")
        self.clear_contents.clicked.connect(self._clear_contents)
        stats_row = QHBoxLayout()
        stats_row.addWidget(self.contents_stats, 1)
        stats_row.addWidget(self.clear_contents)
        contents_form = QFormLayout(self.contents)
        contents_form.addRow("Index:", self.contents_index)
        contents_form.addRow("", about_index)
        contents_form.addRow("Stored:", stats_row)
        self.contents.setVisible(session.has_documents)
        tab = QWidget()
        column = QVBoxLayout(tab)
        column.addLayout(form)
        column.addWidget(self.keep_options)
        column.addWidget(self.online)
        column.addWidget(self._environment_box())
        column.addWidget(self.contents)
        column.addStretch(1)
        return tab

    def _appearance_tab(self) -> QWidget:
        """The keep's icon and colors (#557, #558, #572)."""
        self._edits: dict[str, str] | None = None
        """The scheme's colors as changed and not yet saved (#572)."""
        self._legacy: dict[str, str] | None = None
        """Colors the keep had in ``keep.toml`` (#558), offered until the window closes."""
        self._schemes: dict[str, ColorScheme] = {}
        form = QFormLayout()
        # The keep's icon (#557): its own, else its theme's; the Open Keep dialog shows it.
        self.keep_icon = QPushButton("Change\u2026")
        self.keep_icon.setObjectName("keep_icon")
        self.keep_icon.setToolTip("The keep's icon in the Open Keep dialog")
        self.keep_icon.clicked.connect(self.change_keep_icon)
        self.keep_icon_theme = QPushButton("Use the theme's")
        self.keep_icon_theme.setObjectName("keep_icon_theme")
        self.keep_icon_theme.clicked.connect(lambda: self.set_keep_icon(IconChoice()))
        icon_row = QHBoxLayout()
        icon_row.setContentsMargins(0, 0, 0, 0)
        icon_row.addWidget(self.keep_icon)
        icon_row.addWidget(self.keep_icon_theme)
        icon_row.addStretch(1)
        form.addRow("Icon:", icon_row)
        self.color_mode = QComboBox()
        self.color_mode.setObjectName("color_mode")
        self.color_mode.setToolTip(
            "The keep's colors, shown while its window is the active one: the system's own "
            "(following its light or dark mode, or always light or dark), or a color scheme: "
            "Tagalot's, the theme's, or yours."
        )
        self.color_mode.currentIndexChanged.connect(self._scheme_chosen)
        form.addRow("Colors:", self.color_mode)
        self.custom_colors = QGroupBox("The scheme's colors")
        custom_form = QFormLayout(self.custom_colors)
        self.color_buttons: dict[str, QPushButton] = {}
        for role, label in COLOR_LABELS.items():
            button = QPushButton()
            button.setObjectName(f"color_{role}")
            button.clicked.connect(lambda _=False, r=role: self._choose_color(r))
            self.color_buttons[role] = button
            custom_form.addRow(f"{label}:", button)
        self.colors_warning = QLabel()
        self.colors_warning.setObjectName("colors_warning")
        self.colors_warning.setWordWrap(True)
        self.colors_warning.setStyleSheet(f"color: {css(ERROR)};")
        custom_form.addRow("", self.colors_warning)
        self.save_scheme_button = QPushButton("Save")
        self.save_scheme_button.setObjectName("save_scheme")
        self.save_scheme_button.setToolTip("Save the changes to this scheme (only your own)")
        self.save_scheme_button.clicked.connect(self.save_scheme)
        self.save_scheme_as_button = QPushButton("Save As…")
        self.save_scheme_as_button.setObjectName("save_scheme_as")
        self.save_scheme_as_button.setToolTip(
            "Save these colors as a new scheme of your own, offered in all your keeps"
        )
        self.save_scheme_as_button.clicked.connect(self.save_scheme_as)
        self.scheme_state = QLabel()
        self.scheme_state.setObjectName("scheme_state")
        self.scheme_state.setWordWrap(True)
        scheme_buttons = QHBoxLayout()
        scheme_buttons.setContentsMargins(0, 0, 0, 0)
        scheme_buttons.addWidget(self.save_scheme_button)
        scheme_buttons.addWidget(self.save_scheme_as_button)
        scheme_buttons.addWidget(self.scheme_state, 1)
        custom_form.addRow("", scheme_buttons)
        hint = QLabel(
            "Changes show at once and are kept when you save them. Tagalot works out the rest "
            "(borders, alternate rows, disabled text) from these. Your system's file dialogs "
            "and the window's title bar keep the system's look."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(placeholder-text);")
        custom_form.addRow("", hint)
        # The keep's fonts (#565): titles, notes, and text, and the text's size.
        self.fonts_box = QGroupBox("Fonts")
        fonts_form = QFormLayout(self.fonts_box)
        self.font_buttons: dict[str, QPushButton] = {}
        self._google_here: set[str] = set()
        """The Google Fonts already in the keep folder (#566), found as the picker opens."""
        self.system_font_buttons: dict[str, QPushButton] = {}
        self.theme_font_buttons: dict[str, QPushButton] = {}
        self.download_font_buttons: dict[str, QPushButton] = {}
        for place, (label, tip) in FONT_LABELS.items():
            button = QPushButton()
            button.setObjectName(f"font_{place}")
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, p=place: self.change_font(p))
            theme = QPushButton("Use the theme's")
            theme.setObjectName(f"theme_font_{place}")
            theme.setToolTip("The font the keep's theme suggests (#567)")
            theme.clicked.connect(lambda _=False, p=place: self.set_font(p, None))
            system = QPushButton("Use the system's")
            system.setObjectName(f"system_font_{place}")
            system.clicked.connect(lambda _=False, p=place: self.use_system_font(p))
            download = QPushButton()
            download.setObjectName(f"download_font_{place}")
            download.setVisible(False)
            download.clicked.connect(lambda _=False, p=place: self.download_theme_font(p))
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(button, 1)
            row.addWidget(download)
            row.addWidget(theme)
            row.addWidget(system)
            fonts_form.addRow(f"{label}:", row)
            self.font_buttons[place] = button
            self.theme_font_buttons[place] = theme
            self.system_font_buttons[place] = system
            self.download_font_buttons[place] = download
        self.text_size = QSpinBox()
        self.text_size.setObjectName("text_size")
        self.text_size.setRange(*TEXT_SIZES)
        self.text_size.setSingleStep(5)
        self.text_size.setSuffix("%")
        self.text_size.setKeyboardTracking(False)
        self.text_size.setToolTip(
            "The text's size, in percent of the system's; titles and notes follow it"
        )
        self.text_size.valueChanged.connect(self._text_size_changed)
        fonts_form.addRow("Text size:", self.text_size)
        self.fonts_note = QLabel()
        self.fonts_note.setObjectName("fonts_note")
        self.fonts_note.setWordWrap(True)
        self.fonts_note.setStyleSheet("color: palette(placeholder-text);")
        fonts_form.addRow("", self.fonts_note)
        tab = QWidget()
        column = QVBoxLayout(tab)
        column.addLayout(form)
        column.addWidget(self.custom_colors)
        column.addWidget(self.fonts_box)
        column.addStretch(1)
        return tab

    # --- fonts (#565) ---

    def _show_fonts(self) -> None:
        session = self.session
        config = session.keep.config
        suggested = getattr(session.theme, "fonts", {})
        _, places = look_of(config, session.font_files, session.theme)
        notes = []
        for place, button in self.font_buttons.items():
            shown = places[place]
            label = FONT_LABELS[place][0]
            if shown.origin == "keep" and shown.value == SYSTEM_FONT:
                button.setText("The system's")
            elif shown.origin == "keep" and shown.family is None:
                button.setText(f"{shown.value} (not here)")
                notes.append(
                    f"{label}: {missing_text(str(shown.value))}, so the system's font shows."
                )
            elif shown.origin == "theme" and shown.family is not None:
                button.setText(f"{shown.family} (the theme's)")
            elif shown.family is not None:
                button.setText(shown.family)
            else:
                button.setText("The system's")
            if shown.origin == "theme" and shown.missing:
                notes.append(theme_note(label, shown))
            font = QFont(self.font())
            if shown.family:
                font.setFamilies([shown.family])  # shown in itself
            button.setFont(font)
            chosen = config.fonts.get(place)
            self.theme_font_buttons[place].setVisible(place in suggested)
            self.theme_font_buttons[place].setEnabled(chosen is not None)
            system_shown = chosen == SYSTEM_FONT or (chosen is None and place not in suggested)
            self.system_font_buttons[place].setEnabled(not system_shown)
            offer = download_offer(shown) if shown.origin == "theme" else None
            self.download_font_buttons[place].setVisible(offer is not None)
            if offer is not None:
                self.download_font_buttons[place].setText(f"Download {offer.name}")
                self.download_font_buttons[place].setToolTip(
                    f"Download the theme's {offer.name} into the keep folder, from "
                    f"{GOOGLE_HOST} (Google Fonts' repository on GitHub)"
                )
        blocked = self.text_size.blockSignals(True)
        self.text_size.setValue(config.text_size or self._theme_text_size())
        self.text_size.blockSignals(blocked)
        self.fonts_note.setText(" ".join(notes))
        self.fonts_note.setVisible(bool(notes))

    def _theme_text_size(self) -> int:
        """The text size without the keep's own: the theme's suggestion, else 100%."""
        return getattr(self.session.theme, "text_size", None) or 100

    def download_theme_font(self, place: str) -> None:
        """Download the Google Font the theme suggests for a place (#567), into the keep."""
        session = self.session
        _, places = look_of(session.keep.config, session.font_files, session.theme)
        offer = download_offer(places[place])
        if offer is None:
            return
        self.download_font_buttons[place].setEnabled(False)

        def done() -> None:
            self.download_font_buttons[place].setEnabled(True)
            self._show_fonts()

        self._run(
            lambda: session.download_google_font(offer),
            f"Downloaded {offer.name} from {GOOGLE_HOST}.",
            then=done,
        )

    def change_font(self, place: str) -> None:
        """Pick a font for a place: an installed family, a Google Font downloaded into the
        keep (#566), or a file copied into the keep."""
        session = self.session

        def show(found: tuple[list[str], set[str]]) -> None:
            if not shiboken6.isValid(self):
                return
            samples, self._google_here = found
            look, _ = look_of(session.keep.config, session.font_files, session.theme)
            choice = self.choose_font(place, getattr(look, place), samples)
            if choice is not None:
                self.set_font(place, choice.value or choice.family, choice.file)

        run_in_pool(
            lambda: (sample_titles(session), session.google_fonts_here()),
            on_done=show,
            pool=self._pool,
        )

    def choose_font(self, place: str, current: str | None, samples: list[str]) -> FontChoice | None:
        """Ask for a font (tests replace this)."""
        picker = FontPicker(
            place,
            current,
            samples,
            self,
            here=self._google_here,
            download=self.session.download_google_font,
        )
        return picker.choice if picker.exec() == QDialog.DialogCode.Accepted else None

    def set_font(self, place: str, family: str | None, file: Path | None = None) -> None:
        """Give a place a family, a font file (copied into the keep folder), or the
        system's font (both None); saved in ``keep.toml`` and shown at once."""
        session = self.session
        fonts: dict[str, str | None] = dict(session.keep.config.fonts)
        size = session.keep.config.text_size

        def work() -> None:
            value = session.font_file(file) if file is not None else family
            session.set_fonts({**fonts, place: value}, size)

        label = FONT_LABELS[place][0].lower()
        if file is not None:
            message = f"The {label} font: {file.name}."
        else:
            named = {None: "the theme's", SYSTEM_FONT: "the system's"}
            if place not in getattr(session.theme, "fonts", {}):
                named[None] = "the system's"
            message = f"The {label} font: {named.get(family, family)}."
        self._run(work, message, then=self._show_fonts)

    def use_system_font(self, place: str) -> None:
        """The system's font for a place: over the theme's suggestion, when it has one."""
        suggested = getattr(self.session.theme, "fonts", {})
        self.set_font(place, SYSTEM_FONT if place in suggested else None)

    def _text_size_changed(self, value: int) -> None:
        session = self.session
        fonts = dict(session.keep.config.fonts)
        size = None if value == self._theme_text_size() else value  # the theme's again
        self._run(
            lambda: session.set_fonts(fonts, size),
            f"Text size: {value}%.",
            then=self._show_fonts,
        )

    # --- colors (#558) and color schemes (#572) ---

    def _fill_schemes(self) -> None:
        """The Colors list: the system's looks, then Tagalot's schemes, the theme's, the
        user's, and the keep's own colors from ``keep.toml`` or a scheme it names that's
        gone."""
        config = self.session.keep.config
        schemes = self.session.color_schemes()
        self._schemes = {s.id: s for s in schemes}
        combo = self.color_mode
        blocked = combo.blockSignals(True)
        combo.clear()
        for label, mode in COLOR_CHOICES:
            combo.addItem(label, mode)
        theme_prefix = f"{self.session.theme.id}."
        groups = (
            [s for s in schemes if s.id in BUILT_IN_IDS],
            [s for s in schemes if s.id.startswith(theme_prefix)],
            [s for s in schemes if s.id.startswith(USER)],
        )
        for group in groups:
            if group:
                combo.insertSeparator(combo.count())
            for scheme in group:
                combo.addItem(scheme.name, scheme.id)
                if scheme.id.startswith(USER):
                    combo.setItemData(combo.count() - 1, "Yours", Qt.ItemDataRole.ToolTipRole)
        if self._legacy is not None:
            combo.insertSeparator(combo.count())
            combo.addItem("This keep's colors", CUSTOM)
            combo.setItemData(
                combo.count() - 1,
                "Colors saved in the keep before schemes; Save As keeps them as a scheme",
                Qt.ItemDataRole.ToolTipRole,
            )
        if config.colors not in self._schemes and config.colors not in (*NATIVE, CUSTOM):
            combo.insertSeparator(combo.count())
            combo.addItem(f"{config.colors} (not found: the system's colors)", config.colors)
        combo.setCurrentIndex(max(combo.findData(config.colors), 0))
        combo.blockSignals(blocked)

    def _show_colors(self) -> None:
        config = self.session.keep.config
        if config.colors == CUSTOM:
            self._legacy = dict(config.custom_colors)
        self._fill_schemes()
        scheme_id = self.color_mode.currentData()
        colors = self._scheme_colors(scheme_id)
        self.custom_colors.setVisible(colors is not None)
        shown = self._edits or colors or starting_colors()
        for role, button in self.color_buttons.items():
            swatch = QPixmap(28, 14)
            swatch.fill(QColor(shown[role]))
            button.setIcon(QIcon(swatch))
            button.setText(shown[role])
        problems = hard_to_read(shown) if colors is not None else []
        self.colors_warning.setText(" ".join(problems))
        self.colors_warning.setVisible(bool(problems))
        own = isinstance(scheme_id, str) and scheme_id.startswith(USER)
        self.save_scheme_button.setVisible(own)
        self.save_scheme_button.setEnabled(own and self._edits is not None)
        self.save_scheme_as_button.setEnabled(colors is not None)
        if self._edits is not None:
            state = (
                "Changed: Save or Save As to keep it." if own else "Changed: Save As to keep it."
            )
        elif scheme_id == CUSTOM:
            state = "Save As keeps these colors as a scheme of yours."
        else:
            state = ""
        self.scheme_state.setText(state)

    def _scheme_colors(self, scheme_id: object) -> dict[str, str] | None:
        """A scheme's colors (None for the system's looks and a scheme that's gone)."""
        if scheme_id == CUSTOM:
            return {**starting_colors(), **(self._legacy or {})}
        scheme = self._schemes.get(scheme_id) if isinstance(scheme_id, str) else None
        return scheme_colors(scheme) if scheme is not None else None

    def _scheme_chosen(self) -> None:
        scheme_id = self.color_mode.currentData()
        config = self.session.keep.config
        if scheme_id == config.colors:
            return
        if self._edits is not None:
            if not self.discard_changes():
                blocked = self.color_mode.blockSignals(True)
                self.color_mode.setCurrentIndex(max(self.color_mode.findData(config.colors), 0))
                self.color_mode.blockSignals(blocked)
                return
            self._edits = None
        if scheme_id == CUSTOM:
            self.save_colors(CUSTOM, self._legacy)
        else:
            self.save_colors(scheme_id, None)

    def _choose_color(self, role: str) -> None:
        colors = self._edits or self._scheme_colors(self.color_mode.currentData())
        if colors is None:
            return
        chosen = self.choose_color(COLOR_LABELS[role], colors[role])
        if chosen is not None and chosen != colors[role]:
            self._edits = {**colors, role: chosen}
            apply_colors(CUSTOM, self._edits)  # a preview, until it's saved
            self._show_colors()

    def choose_color(self, label: str, current: str) -> str | None:
        """Ask for a color (tests replace this): ``#rrggbb``, or None."""
        chosen = QColorDialog.getColor(QColor(current), self, label)
        return chosen.name() if chosen.isValid() else None

    def ask_scheme_name(self, suggested: str) -> str | None:
        """Ask for a new scheme's name (tests replace this), or None."""
        name, ok = QInputDialog.getText(self, "Save As", "The new scheme's name:", text=suggested)
        return name if ok else None

    def discard_changes(self) -> bool:
        """Whether to drop the scheme's unsaved changes (tests replace this)."""
        answer = QMessageBox.question(
            self,
            "Unsaved colors",
            "The scheme's colors were changed but not saved. Discard the changes?",
        )
        return answer == QMessageBox.StandardButton.Yes

    def save_colors(self, mode: str, custom: Mapping[str, str] | None) -> None:
        """Show the keep's colors at once, and save them in ``keep.toml``: a look of
        ``COLOR_MODES`` or a scheme's id."""
        try:
            checked = with_colors(self.session.keep.config, mode, custom)
        except KeepError as e:
            self.colors_warning.setText(str(e))
            self.colors_warning.setVisible(True)
            return
        apply_colors(*resolve_colors(checked, self.session.theme))  # before it's saved
        session = self.session
        labels = {m: label for label, m in COLOR_CHOICES}
        name = labels.get(mode) or getattr(self._schemes.get(mode), "name", mode)
        self._run(
            lambda: session.set_colors(checked.colors, checked.custom_colors),
            f"Colors: {name}." if mode != CUSTOM else "Colors: this keep's own.",
            then=self._show_colors,
        )

    def save_scheme(self) -> None:
        """Save the changes to the user's own scheme."""
        scheme = self._schemes.get(self.color_mode.currentData())
        if scheme is None or not scheme.id.startswith(USER) or self._edits is None:
            return
        self._save_scheme(scheme.name, self._edits, scheme.id)

    def save_scheme_as(self) -> None:
        """Save the colors shown as a new scheme of the user's, and give the keep it."""
        scheme_id = self.color_mode.currentData()
        colors = self._edits or self._scheme_colors(scheme_id)
        if colors is None:
            return
        scheme = self._schemes.get(scheme_id)
        name = self.ask_scheme_name(f"My {scheme.name}" if scheme else "My colors")
        if name is None:
            return
        problem = name_problem(name)
        if problem is not None:
            self.colors_warning.setText(problem)
            self.colors_warning.setVisible(True)
            return
        self._save_scheme(name.strip(), colors, None)

    def _save_scheme(self, name: str, colors: Mapping[str, str], scheme_id: str | None) -> None:
        session = self.session

        def saved() -> None:
            self._edits = None
            if session.keep.config.colors != CUSTOM:
                self._legacy = None  # kept as a scheme now
            apply_colors(*session.colors)
            self._show_colors()

        self._run(
            lambda: session.save_color_scheme(name, colors, scheme_id),
            f"Saved the color scheme {name}.",
            then=saved,
        )

    def colors_preview(self) -> tuple[str, dict[str, str]] | None:
        """The scheme's unsaved colors being shown, for the keep's windows to keep showing
        them when they become active; None when there are none."""
        return (CUSTOM, dict(self._edits)) if self._edits is not None else None

    def drop_color_changes(self) -> None:
        """Forget the scheme's unsaved changes, and show the keep's saved colors again."""
        if self._edits is not None:
            self._edits = None
            apply_colors(*self.session.colors)
            self._show_colors()

    def closeEvent(self, event: QCloseEvent) -> None:
        # Asked only when the user closes the window; a keep closing drops the changes.
        if self._edits is not None and event.spontaneous() and not self.discard_changes():
            event.ignore()
            return
        self.drop_color_changes()
        super().closeEvent(event)

    def _notes_tab(self) -> QWidget:
        about = QLabel(
            "Keys listed here never become Extra fields when Tagalot reads notes. One key "
            "per line; case doesn't matter. Changes apply at the next scan (F5), which "
            "reads the notes again."
        )
        about.setWordWrap(True)
        self.keep_ignore_keys = QPlainTextEdit()
        self.keep_ignore_keys.setObjectName("keep_ignore_keys")
        self.keep_ignore_keys.setPlaceholderText("mood-board")
        self.keep_ignore_keys.setToolTip(
            "Saved in this keep (keep.toml), so they go where the keep goes. Ignore this key "
            "in every note\u2026 on an item's Extra field adds one. Saved when you leave the "
            "box."
        )
        self.user_ignore_keys = QPlainTextEdit()
        self.user_ignore_keys.setObjectName("user_ignore_keys")
        self.user_ignore_keys.setToolTip(
            "Saved for you on this computer (settings.toml): every keep you open ignores "
            "them. Saved when you leave the box."
        )
        for box in (self.keep_ignore_keys, self.user_ignore_keys):
            box.installEventFilter(self)
        built_in = QLabel(", ".join(sorted(DEFAULT_IGNORED_KEYS)))
        built_in.setToolTip("Keys editors add for themselves; always ignored")
        built_in.setWordWrap(True)
        self.notes_error = QLabel()
        self.notes_error.setObjectName("notes_error")
        self.notes_error.setStyleSheet(f"color: {css(ERROR)};")
        self.notes_error.setWordWrap(True)
        form = QFormLayout()
        form.addRow("In this keep:", self.keep_ignore_keys)
        form.addRow("In all your keeps:", self.user_ignore_keys)
        form.addRow("Always:", built_in)
        group = QGroupBox("Keys to ignore in notes")
        column = QVBoxLayout(group)
        column.addWidget(about)
        column.addLayout(form)
        column.addWidget(self.notes_error)

        folder_about = QLabel(
            "One folder of Markdown notes for items that have no folder of their own "
            "(actors, authors, a music artist). Each note is matched to an item by its title "
            "(or file name) and aliases, and typed by its tagalot-type key or its subfolder "
            "(Actors/Keanu Reeves.md). Nothing in it is read as anything but notes."
        )
        folder_about.setWordWrap(True)
        self.notes_folder = QLabel()
        self.notes_folder.setObjectName("notes_folder")
        self.notes_folder.setWordWrap(True)
        self.notes_folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        choose = QPushButton("Choose\u2026")
        choose.setToolTip(
            "A folder inside a watched folder, or one of its own (then watched for notes only)"
        )
        choose.clicked.connect(self._choose_notes_folder)
        self.no_notes_folder = QPushButton("None")
        self.no_notes_folder.setToolTip("Stop using a notes folder (its files stay as they are)")
        self.no_notes_folder.clicked.connect(lambda: self.set_notes_folder(None))
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.notes_folder, 1)
        folder_row.addWidget(choose)
        folder_row.addWidget(self.no_notes_folder)
        self.notes_writable = QCheckBox("Let Save as note… add new notes to this folder")
        self.notes_writable.setObjectName("notes_writable")
        self.notes_writable.setToolTip(
            "Off: Tagalot never writes here. On: Save as note… on an item (and only "
            "that) writes its note as a new file here. It never changes or replaces a file, "
            "whether or not the watched folder it's in lets Write to file… change files."
        )
        self.notes_writable.clicked.connect(self._toggle_notes_writable)
        self.notes_write_back = QCheckBox("Let Write to file… change notes' front matter here")
        self.notes_write_back.setObjectName("notes_write_back")
        self.notes_write_back.setToolTip(
            "Off: Write to file… leaves notes here alone. On: Write to file… on an item (and "
            "only that) writes its tags and the fields you edited into its note's front matter "
            "here, after a preview, keeping a copy in the keep's backups folder (#501)."
        )
        self.notes_write_back.clicked.connect(self._toggle_notes_write_back)
        self.new_notes = QComboBox()
        self.new_notes.setObjectName("new_notes")
        self.new_notes.addItem("In a folder for each type (Actors/Keanu Reeves.md)", "folders")
        self.new_notes.addItem("All in the notes folder, typed by tagalot-type", "one-folder")
        self.new_notes.setToolTip("Where Save as note… puts new notes")
        self.new_notes.activated.connect(self._choose_new_notes)
        new_row = QHBoxLayout()
        new_row.addWidget(QLabel("New notes go:"))
        new_row.addWidget(self.new_notes)
        new_row.addStretch(1)
        folder_group = QGroupBox("Notes folder")
        folder_column = QVBoxLayout(folder_group)
        folder_column.addWidget(folder_about)
        folder_column.addLayout(folder_row)
        folder_column.addWidget(self.notes_writable)
        folder_column.addWidget(self.notes_write_back)
        folder_column.addLayout(new_row)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(folder_group)
        layout.addWidget(group)
        layout.addWidget(self._aliases_group())
        layout.addStretch(1)
        return tab

    def _aliases_group(self) -> QGroupBox:
        """Your own key aliases (#411): keys read as fields, per keep and per user."""
        about = QLabel(
            "Keys a theme doesn't know become Extra fields. List a key here, then = and the "
            "field it fills, by its name or label (synopsis = Description), to read it as that "
            "field in notes and Markdown files. A file's own key for the field wins. Changes "
            "apply at the next scan (F5)."
        )
        about.setWordWrap(True)
        self.keep_key_aliases = QPlainTextEdit()
        self.keep_key_aliases.setObjectName("keep_key_aliases")
        self.keep_key_aliases.setPlaceholderText("synopsis = Description")
        self.keep_key_aliases.setToolTip(
            "Saved in this keep (keep.toml), so they go where the keep goes; they win over "
            "yours for the same key. Read this key as a field\u2026 on an item's Extra field "
            "adds one. Saved when you leave the box."
        )
        self.user_key_aliases = QPlainTextEdit()
        self.user_key_aliases.setObjectName("user_key_aliases")
        self.user_key_aliases.setToolTip(
            "Saved for you on this computer (settings.toml): every keep you open reads them. "
            "Saved when you leave the box."
        )
        for box in (self.keep_key_aliases, self.user_key_aliases):
            box.setMaximumHeight(80)
            box.installEventFilter(self)
        self.aliases_error = QLabel()
        self.aliases_error.setObjectName("aliases_error")
        self.aliases_error.setStyleSheet(f"color: {css(ERROR)};")
        self.aliases_error.setWordWrap(True)
        form = QFormLayout()
        form.addRow("In this keep:", self.keep_key_aliases)
        form.addRow("In all your keeps:", self.user_key_aliases)
        group = QGroupBox("Keys to read as fields")
        column = QVBoxLayout(group)
        column.addWidget(about)
        column.addLayout(form)
        column.addWidget(self.aliases_error)
        return group

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
        self.archives.setPlainText("\n".join(root.archives))
        self.watch_button.setText("Stop watching" if root.watched else "Watch again")
        self.watch_button.setToolTip(
            "Stop scanning it; its items stay (shown offline) until you watch it again"
            if root.watched
            else "Scan it again; its items reconnect"
        )
        self.scan_button.setEnabled(root.watched)
        self.writable.setChecked(root.writable)
        self.assets.setText(root.assets)
        keep_assets = self.session.keep.config.markdown_assets
        self.assets.setPlaceholderText(
            f"The keep's: {keep_assets}" if keep_assets else "None: only beside each file"
        )
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

    # --- the keep's environment (#544) ---

    def _environment_box(self) -> QGroupBox:
        """**Environment**: the keep's variables for this user (an online service's key),
        kept in their ``settings.toml``, never in the keep."""
        box = QGroupBox("Environment")
        self.env_list = QListWidget()
        self.env_list.setMaximumHeight(self.fontMetrics().lineSpacing() * 5 + 12)
        self.env_list.currentItemChanged.connect(lambda *_: self._env_buttons())
        self.env_list.itemDoubleClicked.connect(lambda _: self.set_env_value())
        self.env_show = QCheckBox("Show values")
        self.env_show.toggled.connect(lambda _: self.show_environment())
        self.env_set = QPushButton("Set\u2026")
        self.env_set.setToolTip("Give the selected variable a value")
        self.env_set.clicked.connect(self.set_env_value)
        self.env_add = QPushButton("Add\u2026")
        self.env_add.setToolTip("Add a variable by name")
        self.env_add.clicked.connect(self.add_env)
        self.env_remove = QPushButton("Remove")
        self.env_remove.clicked.connect(self.remove_env)
        about = QLabel(
            "Variables such as an online service's key, for this keep. Yours alone, on this "
            "computer: they're kept in your settings, not in the keep, and win over the "
            "system's environment variables of the same name."
        )
        about.setWordWrap(True)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.env_set)
        buttons.addWidget(self.env_add)
        buttons.addWidget(self.env_remove)
        buttons.addStretch(1)
        buttons.addWidget(self.env_show)
        layout = QVBoxLayout(box)
        layout.addWidget(about)
        layout.addWidget(self.env_list)
        layout.addLayout(buttons)
        self.show_environment()
        return box

    def show_environment(self) -> None:
        """List the keep's variables (values hidden unless **Show values**), then those its
        theme's services need that aren't set."""
        current = self.env_list.currentItem()
        selected = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        env = self.session.environment()
        needs = dict(needed_env(self.session.theme.online_sources))
        self.env_list.clear()
        for name, value in sorted(env.items()):
            shown = value if self.env_show.isChecked() else "\u2022" * 8
            item = QListWidgetItem(f"{name} = {shown}")
            if name in needs:
                item.setToolTip(f"{needs[name]} uses it")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.env_list.addItem(item)
        for name, sources in needs.items():
            if name in env:
                continue
            item = QListWidgetItem(f"{name}: not set ({sources} needs it)")
            item.setForeground(self.palette().color(QPalette.ColorRole.PlaceholderText))
            item.setToolTip(f"Select it and click Set\u2026 to give {sources} your key")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.env_list.addItem(item)
        for row in range(self.env_list.count()):
            if self.env_list.item(row).data(Qt.ItemDataRole.UserRole) == selected:
                self.env_list.setCurrentRow(row)
        self._env_buttons()

    def _env_buttons(self) -> None:
        current = self.env_list.currentItem()
        name = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self.env_set.setEnabled(name is not None)
        self.env_remove.setEnabled(name is not None and name in self.session.environment())

    def _env_name(self) -> str | None:
        current = self.env_list.currentItem()
        name = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        return name if isinstance(name, str) else None

    def ask_env_name(self) -> str | None:
        """The name of a variable to add (tests replace it)."""
        name, ok = QInputDialog.getText(self, "Add a variable", "Name, such as TAGALOT_TMDB_TOKEN:")
        return name.strip() or None if ok else None

    def ask_env_value(self, name: str) -> str | None:
        """A variable's value, typed hidden; ``None`` if cancelled (tests replace it)."""
        value, ok = QInputDialog.getText(
            self, f"Set {name}", f"Value of {name}:", QLineEdit.EchoMode.Password
        )
        return value if ok else None

    def set_env_value(self) -> None:
        name = self._env_name()
        if name is not None:
            self._save_env(name)

    def add_env(self) -> None:
        name = self.ask_env_name()
        if name is None:
            return
        if not ENV_NAME.fullmatch(name):  # said before asking for its value
            QMessageBox.warning(
                self,
                "Environment",
                f"{name!r} can't name an environment variable: use letters, digits, and _, "
                "not starting with a digit.",
            )
            return
        self._save_env(name)

    def remove_env(self) -> None:
        name = self._env_name()
        if name is not None and name in self.session.environment():
            self._change_env(name, None, f"Removed {name} from this keep's environment.")

    def _save_env(self, name: str) -> None:
        value = self.ask_env_value(name)
        if value is None:
            return
        if not value.strip():
            self._change_env(name, None, f"Removed {name} from this keep's environment.")
        else:
            self._change_env(name, value, f"Set {name} for this keep.")

    def _change_env(self, name: str, value: str | None, message: str) -> None:
        settings, keep_id = self.session.settings, self.session.keep.config.id
        try:
            settings.set_env(keep_id, name, value)
        except ValueError as e:
            QMessageBox.warning(self, "Environment", str(e))
            return
        settings_path = self.settings_path
        self.show_environment()
        self.tokens.setText(
            tokens_text(self.session.theme.online_sources, self.session.environment())
        )
        self.tokens.setVisible(bool(self.tokens.text()))
        self.online_sends.setText(
            consent_text(self.session.online_sources(), self.session.keep.config.online_find_by)
        )
        self._run(lambda: save_settings(settings, settings_path), message)

    def _show_settings(self) -> None:
        """The Thumbnails and Keep tabs, from the saved configuration."""
        config = self.session.keep.config
        self.keep_name.setText(config.name)
        self.keep_icon.setIcon(icon(self.session.keep_icon()))
        self.keep_icon_theme.setEnabled(config.icon is not None)
        self._show_colors()
        self._show_fonts()
        self.keep_assets.setText(config.markdown_assets)
        if not self.see_through.hasFocus():
            self.see_through.setPlainText("\n".join(config.see_through))
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
        self.find_by.blockSignals(True)
        self.find_by.setCurrentIndex(self.find_by.findData(config.online_find_by))
        self.find_by.blockSignals(False)
        self.online_sends.setText(
            consent_text(self.session.online_sources(), config.online_find_by)
        )
        self.contents_index.setCurrentIndex(self.contents_index.findData(config.contents_index))
        self.show_contents_stats()
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
        self.show_note_keys()
        self.show_cache_stats()

    def show_note_keys(self) -> None:
        """The Notes tab, as saved (a box being typed in is left alone)."""
        config = self.session.keep.config
        root = next((r for r in config.roots if r.id == config.notes_root), None)
        if root is None:
            self.notes_folder.setText("None")
        else:
            where = local_path(self.session.root_path(root.id), config.notes_path)
            whole = "all of it" if not config.notes_path else config.notes_path
            self.notes_folder.setText(f"{where}\n(in {root.name}: {whole})")
        self.no_notes_folder.setEnabled(root is not None)
        self.notes_writable.setEnabled(root is not None)
        self.notes_writable.setChecked(root is not None and config.notes_writable)
        self.notes_write_back.setEnabled(root is not None)
        self.notes_write_back.setChecked(root is not None and config.notes_write_back)
        self.new_notes.setEnabled(root is not None)
        self.new_notes.setCurrentIndex(max(0, self.new_notes.findData(config.new_notes)))
        lists = (
            (self.keep_ignore_keys, self.session.keep.config.note_ignore_keys),
            (self.user_ignore_keys, self.session.settings.note_ignore_keys),
        )
        for box, keys in lists:
            if not box.hasFocus():
                box.setPlainText("\n".join(keys))
        alias_lists = (
            (self.keep_key_aliases, self.session.keep.config.note_key_aliases),
            (self.user_key_aliases, self.session.settings.note_key_aliases),
        )
        for box, aliases in alias_lists:
            if not box.hasFocus():
                box.setPlainText(alias_lines(aliases))

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
            apply_colors(*(self.colors_preview() or self.session.colors))  # this keep's (#558)
            session = self.session
            apply_fonts(look_of(session.keep.config, session.font_files, session.theme)[0])

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

    def change_keep_icon(self) -> None:
        own = self.session.keep.config.icon
        choice = self.choose_icon(
            "Icon for this keep",
            own or getattr(self.session.theme, "icon", None),
            "Use the theme's icon" if own else None,
        )
        if choice is not None:
            self.set_keep_icon(choice)

    def choose_icon(
        self, title: str, current: str | None, standard: str | None
    ) -> IconChoice | None:
        """Ask for an icon (tests replace this)."""
        picker = IconPicker(title, current, standard, self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return None
        return picker.choice

    def set_keep_icon(self, choice: IconChoice) -> None:
        """Save the keep's icon (a picture is copied into the keep folder first)."""
        session = self.session
        theirs = choice.name is None and choice.picture is None
        self._run(
            lambda: session.set_keep_icon(choice.name, choice.picture),
            "The keep shows its theme's icon again." if theirs else "Saved the keep's icon.",
        )

    def _set_markdown_assets(self, assets: str) -> None:
        config = self.session.keep.config
        try:
            cleaned = with_markdown_assets(config, assets).markdown_assets
        except KeepError as e:
            self._refused(str(e))
            return
        if cleaned == config.markdown_assets:
            return
        session = self.session
        self._run(
            lambda: session.set_markdown_assets(cleaned),
            "Saved the assets folder. Covers are looked for there at the next scan (F5).",
        )

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
            self.online_allowed.emit if on else None,
        )

    def _set_find_by(self, index: int) -> None:
        session = self.session
        find_by = self.find_by.itemData(index)
        label = self.find_by.itemText(index)
        self.online_sends.setText(consent_text(session.online_sources(), find_by))
        self._run(
            lambda: session.set_online_find_by(find_by),
            f"Items are now found online by {label[0].lower()}{label[1:]}.",
        )

    def show_contents_stats(self) -> None:
        """What ``fulltext.db`` holds, read in a worker."""
        session = self.session
        if not session.has_documents:
            return

        def done(stats: ContentsStats | None) -> None:
            if not shiboken6.isValid(self):
                return
            self.contents_stats.setText(describe_contents(stats))
            self.clear_contents.setEnabled(stats is not None and stats.files > 0)

        run_in_pool(session.contents_stats, on_done=done, pool=self._pool)

    def _set_contents_index(self, index: str | None) -> None:
        session = self.session
        if index == session.keep.config.contents_index:
            return
        message = {
            None: "Documents' contents aren't searched now; their text is kept.",
            "words": "Documents' contents are searched by words.",
            "substrings": "Documents' contents are searched by substrings.",
        }[index]
        self._run(lambda: session.set_contents_index(index), message, self.contents_changed.emit)

    def _clear_contents(self) -> None:
        if not self.confirm_clear_contents():
            return
        session = self.session
        self._run(
            session.clear_contents,
            "Deleted the documents' text; their contents aren't searched now.",
            self.contents_changed.emit,
        )

    def confirm_clear_contents(self) -> bool:
        answer = QMessageBox.question(
            self,
            "Clear contents text",
            "Stop searching inside documents, and delete their text and its index? "
            "Turning it on again reads every document again.\n\nNo files on disk are changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

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
        offers_scan = "path" in changes or "exclude" in changes or "archives" in changes
        message = f"Saved {root.name}." + (" Scan it to update its files." if offers_scan else "")
        if "assets" in changes:
            message = f"Saved {root.name}. Covers are looked for there at the next scan (F5)."
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
        """Save the exclude patterns, or a list of keys to ignore in notes, when its box
        loses the focus."""
        if event.type() == QEvent.Type.FocusOut:
            if watched is self.exclude:
                patterns, notes = parse_skip_lines(self.exclude.toPlainText().splitlines())
                self._edit(exclude=patterns, exclude_notes=notes)
            elif watched is self.archives:  # archives read whole (#391, #519)
                lines = self.archives.toPlainText().splitlines()
                self._edit(archives=list(dict.fromkeys(p.strip() for p in lines if p.strip())))
            elif watched is self.keep_ignore_keys:
                self.set_keep_ignore_keys(self.keep_ignore_keys.toPlainText().splitlines())
            elif watched is self.user_ignore_keys:
                self.set_user_ignore_keys(self.user_ignore_keys.toPlainText().splitlines())
            elif watched is self.keep_key_aliases:
                self.set_keep_key_aliases(self.keep_key_aliases.toPlainText().splitlines())
            elif watched is self.user_key_aliases:
                self.set_user_key_aliases(self.user_key_aliases.toPlainText().splitlines())
            elif watched is self.see_through:
                self.set_see_through(self.see_through.toPlainText().splitlines())
        return super().eventFilter(watched, event)

    def _choose_notes_folder(self) -> None:
        folder = self.pick_folder("Choose the notes folder")
        if folder:
            self.use_notes_folder(folder)

    def use_notes_folder(self, folder: str) -> None:
        """Make ``folder`` the notes folder: in place, inside a watched folder; else, if the
        user agrees, watched as a folder of its own for notes only. Then offer a scan."""
        session = self.session
        config = session.keep.config
        found = notes_folder_for(config, session.root_path, folder)
        if found is not None:
            root_id, path = found
            root = next(r for r in config.roots if r.id == root_id)

            def saved() -> None:
                if self.ask_scan(root):
                    self.scan_requested.emit([root_id])

            self._run(
                lambda: session.set_notes_folder(root_id, path),
                f"The notes folder is {folder}; the next scan of {root.name} reads it.",
                saved,
            )
            return
        if not self.ask_notes_root(folder):
            return
        try:
            added = add_root(config, session.keep.dir, "", folder)
            new = next(r for r in added.roots if r.id not in {r.id for r in config.roots})
            updated = with_notes_folder(added, new.id, "")
        except (KeepError, StopIteration) as e:
            self._refused(str(e) or f"{folder} is already watched.")
            return

        def watched() -> None:
            if self.ask_scan(new):
                self.scan_requested.emit([new.id])

        self._run(
            lambda: session.save_config(updated),
            f"Now watching {new.name} for notes only.",
            watched,
        )

    def set_notes_folder(self, root_id: str | None, path: str = "") -> None:
        """Use the notes folder at ``path`` in ``root_id``, or none (``None``)."""
        session = self.session
        self._run(
            lambda: session.set_notes_folder(root_id, path),
            "The keep has no notes folder now; the next scan reads its files as usual."
            if root_id is None
            else "Saved the notes folder; the next scan reads it.",
        )

    def _toggle_notes_writable(self, on: bool) -> None:
        """Let Save as note… write in the notes folder (after asking), or not."""
        if on and not self.ask_notes_writable():
            self.notes_writable.setChecked(False)
            return
        session = self.session
        self._run(
            lambda: session.set_notes_writing(writable=on),
            "Save as note… may add notes to the notes folder."
            if on
            else "Tagalot won't write in the notes folder.",
        )

    def _toggle_notes_write_back(self, on: bool) -> None:
        """Let Write to file… change notes in the notes folder (after asking), or not."""
        if on and not self.ask_notes_write_back():
            self.notes_write_back.setChecked(False)
            return
        session = self.session
        self._run(
            lambda: session.set_notes_writing(write_back=on),
            "Write to file… may change notes in the notes folder."
            if on
            else "Write to file… leaves the notes folder alone.",
        )

    def ask_notes_write_back(self) -> bool:
        config = self.session.keep.config
        root = next((r for r in config.roots if r.id == config.notes_root), None)
        if root is None:
            return False
        folder = local_path(self.session.root_path(root.id), config.notes_path)
        answer = QMessageBox.question(
            self,
            "Let Tagalot change notes in this folder?",
            f"Let Write to file… change the front matter of notes in {folder}?\n\n"
            "Tagalot still writes nothing on its own: only when you choose Write to file… on "
            "items, after showing each change. It changes only the front matter (tags, and "
            "fields you edited), keeps a copy of each note in the keep's backups folder, and "
            "writes nowhere else in the folder it's in.",
        )
        return answer == QMessageBox.StandardButton.Yes

    def ask_notes_writable(self) -> bool:
        config = self.session.keep.config
        root = next((r for r in config.roots if r.id == config.notes_root), None)
        if root is None:
            return False
        folder = local_path(self.session.root_path(root.id), config.notes_path)
        answer = QMessageBox.question(
            self,
            "Let Tagalot add notes to this folder?",
            f"Let Save as note… add new notes to {folder}?\n\n"
            "Tagalot still writes nothing on its own: only when you choose Save as "
            "note… on an item. It only adds new files (and a folder for each type, if "
            "you choose that layout), never changes or replaces one, and writes nowhere "
            "else in the folder it's in.",
        )
        return answer == QMessageBox.StandardButton.Yes

    def _choose_new_notes(self, index: int) -> None:
        layout = str(self.new_notes.itemData(index))
        if layout == self.session.keep.config.new_notes:
            return
        session = self.session
        self._run(
            lambda: session.set_notes_writing(new_notes=layout),
            "Save as note… puts new notes in a folder for each type."
            if layout == "folders"
            else "Save as note… puts new notes in the notes folder itself.",
        )

    def ask_notes_root(self, folder: str) -> bool:
        answer = QMessageBox.question(
            self,
            "Watch this folder for notes?",
            f"{folder} isn't inside a watched folder. Watch it as a folder of its own, "
            "used only for notes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _note_keys(self, lines: list[str]) -> list[str] | None:
        """The keys typed, each once (case aside), or ``None`` (saying why) if one can't
        be ignored."""
        keys: list[str] = []
        try:
            for line in lines:
                if line.strip() and line.strip().casefold() not in {k.casefold() for k in keys}:
                    keys.append(check_key(line))
        except NoteKeyError as e:
            self.notes_error.setText(str(e))
            return None
        self.notes_error.clear()
        return keys

    def _key_aliases(self, lines: list[str]) -> dict[str, str] | None:
        """The aliases typed (``key = field`` a line), or ``None`` (saying why) if one
        can't be read."""
        try:
            aliases = parse_alias_lines(lines)
        except NoteKeyError as e:
            self.aliases_error.setText(str(e))
            return None
        self.aliases_error.clear()
        return aliases

    def set_keep_key_aliases(self, lines: list[str]) -> None:
        """Save the keys this keep reads as fields (``keep.toml``, #411)."""
        aliases = self._key_aliases(lines)
        session = self.session
        if aliases is None or aliases == session.keep.config.note_key_aliases:
            return
        self._run(
            lambda: session.set_note_key_aliases(aliases),
            "Saved the keys this keep reads as fields; they apply at the next scan (F5).",
        )

    def set_user_key_aliases(self, lines: list[str]) -> None:
        """Save the keys every keep reads as fields (``settings.toml``, #411)."""
        aliases = self._key_aliases(lines)
        settings, settings_path = self.session.settings, self.settings_path
        if aliases is None or aliases == settings.note_key_aliases:
            return
        settings.note_key_aliases = aliases
        self._run(
            lambda: save_settings(settings, settings_path),
            "Saved the keys all your keeps read as fields; they apply at each keep's next "
            "scan (F5).",
        )

    def set_keep_ignore_keys(self, lines: list[str]) -> None:
        """Save the keys this keep ignores in notes (``keep.toml``)."""
        keys = self._note_keys(lines)
        session = self.session
        if keys is None or keys == session.keep.config.note_ignore_keys:
            return
        self._run(
            lambda: session.set_note_ignore_keys(keys),
            "Saved the keys this keep ignores in notes; they apply at the next scan (F5).",
        )

    def set_see_through(self, lines: list[str]) -> None:
        """Save the keep's see-through folder names (#459), once the user agreed to lose
        the tagged folder items they take away, if any."""
        names = clean_names(lines)
        session = self.session
        if names == session.keep.config.see_through:
            return

        def counted(tagged: object) -> None:
            if not shiboken6.isValid(self):
                return
            if isinstance(tagged, int) and tagged and not self.confirm_see_through(tagged):
                self.see_through.setPlainText("\n".join(session.keep.config.see_through))
                return
            self._run(
                lambda: session.set_see_through(names),
                "Saved the see-through folders; the next scan (F5) reads the keep's files again.",
            )

        run_in_pool(lambda: session.tagged_hidden_folders(names), on_done=counted, pool=self._pool)

    def confirm_see_through(self, tagged: int) -> bool:
        folders = "1 folder you tagged is" if tagged == 1 else f"{tagged:,} folders you tagged are"
        answer = QMessageBox.question(
            self,
            "See-through folders",
            f"{folders} named like this, or inside one. At the next scan they stop being "
            "items, and their tags go with them; what's in them counts as their parent "
            "folder's.\n\nNo files on disk are changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def set_user_ignore_keys(self, lines: list[str]) -> None:
        """Save the keys every keep ignores in notes (``settings.toml``)."""
        keys = self._note_keys(lines)
        settings, settings_path = self.session.settings, self.settings_path
        if keys is None or keys == settings.note_ignore_keys:
            return
        settings.note_ignore_keys = keys
        self._run(
            lambda: save_settings(settings, settings_path),
            "Saved the keys all your keeps ignore in notes; they apply at each keep's next "
            "scan (F5).",
        )

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


CONTENTS_CHOICES = (("Off", None), ("Words", "words"), ("Substrings", "substrings"))
"""Keep configuration's choices for searching inside documents (§8)."""
FIND_BY_CHOICES = (
    ("IDs only", "ids"),
    ("IDs, or a title with its year", "title-year"),
    ("IDs, or any title", "title"),
)
"""Keep configuration's choices for what may identify an item to a service (#485)."""


def describe_contents(stats: ContentsStats | None) -> str:
    """One line on what the keep holds of its documents' text."""
    if stats is None or not stats.files:
        return "Nothing read yet"
    read = stats.files - stats.failed
    text = f"Text of {read:,} {'document' if read == 1 else 'documents'}"
    text += f" ({stats.pages:,} {'page' if stats.pages == 1 else 'pages'}, "
    text += f"{format_bytes(stats.size)})"
    if stats.failed:
        text += f"; {stats.failed:,} couldn't be read"
    return text


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
        text += f'<br><span style="color:{css(ERROR)}">{html.escape(status.last_error)}</span>'
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
