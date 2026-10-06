"""Main window: navigation, center stack, tagging panel, status bar (DESIGN.md §12).

Layout: a slim toolbar ("Scan now", the keep's name); the navigation pane on the left and
the current view in the center; a dockable tagging panel on the right; a status bar showing
scan progress. Views that arrive in later milestones show a labelled placeholder.
"""

import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeySequence, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QStackedWidget,
    QStyle,
    QToolBar,
    QToolButton,
    QWidget,
)

from tagalot.core.actions import ActionResult
from tagalot.core.activity import SCAN, Problem
from tagalot.core.contents import ContentsResult
from tagalot.core.formats import format_bytes
from tagalot.core.handlers import OPEN, REVEAL, FileToOpen
from tagalot.core.keywords import (
    KeywordInfo,
    any_keywords,
    file_tag_counts,
    keyword_index,
    keyword_key,
    unmatched_keys,
)
from tagalot.core.links import kind_of_file
from tagalot.core.models import ResourceKind
from tagalot.core.online import LookupReport
from tagalot.core.root_admin import RootStatus, add_skipped, root_statuses
from tagalot.core.roots import place_in_roots
from tagalot.core.saved_searches import SavedDefinition, SavedSearchError, list_saved
from tagalot.core.scanjob import ScanReport
from tagalot.core.search_fields import type_plurals, view_spec
from tagalot.core.search_spec import FieldFilter, SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.tags import (
    PATH_SEPARATOR,
    DeleteMode,
    TagError,
    entity_types,
    parse_types,
    scope_conflicts,
    split_tag_path,
    tag_counts,
)
from tagalot.core.thumbnails.cache import CacheStats
from tagalot.core.thumbnails.queue import QueueResult
from tagalot.core.triage import UnlinkedFile, exact_pattern
from tagalot.core.ui_state import load_ui_state, save_ui_state
from tagalot.core.writeback import FileWrite, WritePlan, WriteResult, plan_write_back, write_files
from tagalot.themes.api import Kind, SearchView, entity_label
from tagalot.ui.activity import ActivityPanel
from tagalot.ui.dashboard import DashboardPage
from tagalot.ui.dedupe_view import DedupePage
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.field_editor import open_web_address
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.folder_picker import choose_save_file
from tagalot.ui.keep_config import KeepConfigWindow
from tagalot.ui.keywords_page import KeywordsPage
from tagalot.ui.link_dialog import LinkDialog
from tagalot.ui.lookups import OnlineLookups, lookup_summary
from tagalot.ui.merge_dialog import MergeDialog
from tagalot.ui.models.results import KEYWORDS
from tagalot.ui.navigation import NavigationPane, NavTarget
from tagalot.ui.relate_dialog import RelateDialog
from tagalot.ui.result_table import DEFAULT_HIDDEN
from tagalot.ui.search_view import SearchPage
from tagalot.ui.shortcuts import ShortcutsDialog, help_action
from tagalot.ui.tag_actions import TagActions
from tagalot.ui.tag_manager import TagManagerPage
from tagalot.ui.tag_panel import TagPanel
from tagalot.ui.tag_picker import TagPickerDialog, map_dialog
from tagalot.ui.tag_types_dialog import TagTypesDialog
from tagalot.ui.thumbnails import ThumbnailLoader, clamp_size, size_presets, zoomed
from tagalot.ui.triage import UNTAGGED_TAB, TriagePage
from tagalot.ui.workers import ScanController, run_in_pool
from tagalot.ui.write_back_dialog import WriteBackDialog

logger = logging.getLogger(__name__)

WINDOW_TITLE = "Tagalot"

SUMMARY_DELAY_MS = 150
"""How long the Tags panel's selection summary waits for the selection to settle."""

NAVIGATION_MIN_WIDTH = 170
"""The navigation pane never gets narrower than this, however wide a page wants to be."""

OPENING_PAGE = NavTarget("dashboard", label="Dashboard")
"""What a keep opens on."""

_COMING: dict[str, str] = {}
"""Pages not built yet, by kind: a placeholder says when they come."""


MAX_HISTORY = 100
"""Pages remembered for Back."""

MAX_DETAIL_PAGES = 30
"""Detail pages kept alive; older ones are made again when shown."""


class MainWindow(QMainWindow):
    """The top-level window, for an open keep or (with no session) an empty placeholder.

    Emits :attr:`closed` once the window has actually closed (not when a close was refused
    because a scan is running), so its owner can close the keep.
    """

    _queue_progress = Signal(int, int)
    """From the background thumbnail thread: (done, total)."""
    _queue_done = Signal(object)
    _contents_progress = Signal(int, int)
    _contents_done = Signal(object)
    """From the background thumbnail thread: its ``QueueResult``."""

    closed = Signal()

    def __init__(
        self,
        session: KeepSession | None = None,
        parent: QWidget | None = None,
        scans: ScanController | None = None,
        on_open_other: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.resize(1200, 800)
        if session is None:
            self.setWindowTitle(WINDOW_TITLE)
            placeholder = QLabel("No keep open")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setCentralWidget(placeholder)
            self.statusBar().showMessage("Ready")
            return

        self.setWindowTitle(f"{session.keep.config.name} — {WINDOW_TITLE}")
        self._ui_state = load_ui_state(session.keep.ui_state_path)
        self.scans = scans or ScanController()
        self._pages: dict[NavTarget, QWidget] = {}
        self._saved: dict[int, tuple[str, dict[str, Any]]] = {}
        """Saved searches by id: (name, definition), as last loaded."""
        self._history: list[NavTarget] = []
        self._history_index = -1
        self.thumbnails = ThumbnailLoader(session.thumbnails, self)
        self.thumbnail_size = clamp_size(
            self._ui_state.get("thumbnail_size"),
            session.thumbnail_default,
            session.thumbnail_max,
        )

        # Actions and menus.
        self.scan_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload), "Scan now", self
        )
        self.scan_action.setShortcut(QKeySequence(Qt.Key.Key_F5))
        self.scan_action.setToolTip("Scan every root for new, changed, and missing files (F5)")
        self.scan_action.triggered.connect(self.scan_now)
        close_action = QAction("Close", self)
        close_action.setShortcut(QKeySequence.StandardKey.Close)
        close_action.triggered.connect(self.close)
        keep_menu = self.menuBar().addMenu("&Keep")
        keep_menu.addAction(self.scan_action)
        self.clear_thumbnails_action = QAction("Clear thumbnail cache…", self)
        self.clear_thumbnails_action.setToolTip(
            "Delete every stored thumbnail; they are made again as you browse"
        )
        self.clear_thumbnails_action.triggered.connect(self.clear_thumbnail_cache)
        self.configure_action = QAction("Configure keep\u2026", self)
        self.configure_action.setToolTip("The folders this keep watches, and their status")
        self.configure_action.triggered.connect(self.configure_keep)
        keep_menu.addAction(self.configure_action)
        self.keep_config: KeepConfigWindow | None = None
        keep_menu.addSeparator()
        if on_open_other is not None:
            self.open_other_action = QAction("Open another keep…", self)
            self.open_other_action.setShortcut(QKeySequence.StandardKey.Open)
            self.open_other_action.triggered.connect(on_open_other)
            keep_menu.addAction(self.open_other_action)
        keep_menu.addAction(close_action)

        # Edit: undo and redo tagging and tag operations (DESIGN.md §7).
        self.tag_actions = TagActions(session, self)
        self.files = FileOpener(session, None, self)
        self.files.message.connect(lambda text: self.statusBar().showMessage(text, 8000))
        self.tag_actions.changed.connect(self._tags_changed)
        self.tag_actions.message.connect(self._tag_message)
        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.tag_actions.undo)
        self.redo_action = QAction("Redo", self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.triggered.connect(self.tag_actions.redo)
        edit_menu = self.menuBar().addMenu("&Edit")
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        self.tag_selection_action = QAction("Tag selection…", self)
        self.tag_selection_action.setShortcut(QKeySequence("Ctrl+T"))
        self.tag_selection_action.setToolTip(
            "Type a tag, then Enter to apply it to the selected items (Shift+Enter removes it)"
        )
        self.tag_selection_action.triggered.connect(self.focus_tag_filter)
        edit_menu.addAction(self.tag_selection_action)
        edit_menu.addSeparator()
        self.save_search_action = QAction("Save search\u2026", self)
        self.save_search_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_search_action.setToolTip(
            "Keep this search, with its chips and layout, under SAVED (a saved search's "
            "page is updated in place)"
        )
        self.save_search_action.triggered.connect(lambda: self.save_search(as_new=False))
        edit_menu.addAction(self.save_search_action)
        self.save_search_as_action = QAction("Save search as\u2026", self)
        self.save_search_as_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.save_search_as_action.triggered.connect(lambda: self.save_search(as_new=True))
        edit_menu.addAction(self.save_search_as_action)
        self._update_undo_actions()

        # Back and forward through the pages shown (DESIGN.md §12).
        self.back_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack), "Back", self
        )
        self.back_action.setShortcuts(
            [QKeySequence(QKeySequence.StandardKey.Back), QKeySequence("Alt+Left")]
        )
        self.back_action.setToolTip("Back to the previous page (Alt+Left)")
        self.back_action.triggered.connect(self.back)
        self.forward_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward), "Forward", self
        )
        self.forward_action.setShortcuts(
            [QKeySequence(QKeySequence.StandardKey.Forward), QKeySequence("Alt+Right")]
        )
        self.forward_action.setToolTip("Forward again (Alt+Right)")
        self.forward_action.triggered.connect(self.forward)
        go_menu = self.menuBar().addMenu("&Go")
        go_menu.addAction(self.back_action)
        go_menu.addAction(self.forward_action)
        self._show_history()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)  # the mouse's back and forward buttons

        # Toolbar.
        toolbar = QToolBar("Main toolbar", self)
        toolbar.setObjectName("main_toolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.addAction(self.back_action)
        toolbar.addAction(self.forward_action)
        toolbar.addSeparator()
        toolbar.addAction(self.scan_action)
        keep_label = QLabel(f"  {session.keep.config.name}")
        keep_label.setToolTip(str(session.keep.dir))
        toolbar.addWidget(keep_label)
        self.addToolBar(toolbar)

        # Navigation and the center stack.
        views = [v.name for v in session.theme.views if isinstance(v, SearchView)]
        self.navigation = NavigationPane(views)
        self.navigation.setMinimumWidth(NAVIGATION_MIN_WIDTH)  # pages can't squeeze it
        self.navigation.set_folded(self._ui_state.get("nav_folded", []))
        self.navigation.navigate.connect(self.show_target)
        self.navigation.folded_changed.connect(self._save_folded)
        self.navigation.saved_menu_requested.connect(self._saved_menu)
        self.stack = QStackedWidget()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.navigation)
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 980])
        self.setCentralWidget(splitter)

        # The tagging panel as a dock.
        self.tags_dock = QDockWidget("Tags", self)
        self.tags_dock.setObjectName("tags_dock")
        self.tag_panel = TagPanel(session)
        self.tag_panel.tag_requested.connect(self.tag_selection)
        self.tag_panel.create_requested.connect(self.create_tag)
        self.tag_panel.search_requested.connect(self.add_tags_to_search)
        # The panel's all / some / none checks follow the current page's selection. Selection
        # changes come in bursts (Shift+arrows), so the summary waits for a short pause.
        self._summary_generation = 0
        self._summary_timer = QTimer(self)
        self._summary_timer.setSingleShot(True)
        self._summary_timer.setInterval(SUMMARY_DELAY_MS)
        self._summary_timer.timeout.connect(self.update_selection_summary)
        self.tags_dock.setWidget(self.tag_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.tags_dock)
        self.resizeDocks([self.tags_dock], [260], Qt.Orientation.Horizontal)
        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.tags_dock.toggleViewAction())
        self.preview_action = QAction("Preview", self)
        self.preview_action.setCheckable(True)
        self.preview_action.setChecked(bool(self._ui_state.get("preview", True)))
        self.preview_action.setToolTip("Show the selected item under the results")
        self.preview_action.toggled.connect(self._preview_toggled)
        view_menu.addAction(self.preview_action)
        # The activity panel: hidden until asked for (View menu or the problem badge).
        self.activity = ActivityPanel(
            lambda: session.keep.config.roots,
            session._known_root_path,
            lambda path: self.files.act(FileToOpen(0, path, False), REVEAL),
            self,
        )
        self.activity.clear_button.clicked.connect(self._clear_problems)
        self.activity.review_keywords.connect(self.review_keywords)
        self._unmatched_keywords: frozenset[str] = frozenset()
        """File keywords matching no tag, as last counted (to tell a scan's new ones)."""
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.activity)
        self.activity.hide()
        activity_action = self.activity.toggleViewAction()
        activity_action.setText("Activity")
        activity_action.setShortcut(QKeySequence("Ctrl+Shift+A"))
        activity_action.setToolTip("Scans, background thumbnails, folders, and problems")
        view_menu.addAction(activity_action)
        self.activity.visibilityChanged.connect(self._activity_shown)
        view_menu.addSeparator()
        self.size_menu = self._make_size_menu()
        view_menu.addMenu(self.size_menu)

        help_menu = self.menuBar().addMenu("&Help")
        self.shortcuts_action = help_action(self)
        self.shortcuts_action.triggered.connect(self.show_shortcuts)
        help_menu.addAction(self.shortcuts_action)

        # Status bar: a message plus a busy indicator while scanning.
        self.thumbnail_status = QLabel()
        self.thumbnail_status.setObjectName("thumbnail_status")
        self.thumbnail_status.setVisible(False)
        self.statusBar().addPermanentWidget(self.thumbnail_status)
        self._queue_progress.connect(self._show_queue)
        self._queue_done.connect(self._queue_finished)
        self.contents_status = QLabel()
        self.contents_status.setObjectName("contents_status")
        self.contents_status.setVisible(False)
        self.statusBar().addPermanentWidget(self.contents_status)
        self._contents_progress.connect(self._show_contents)
        self._contents_done.connect(self._contents_finished)
        self.lookup_status = QLabel()
        self.lookup_status.setObjectName("lookup_status")
        self.lookup_status.setVisible(False)
        self.statusBar().addPermanentWidget(self.lookup_status)
        self.lookups: OnlineLookups | None = None
        if session is not None:
            self.lookups = OnlineLookups(session, self)
            self.lookups.progressed.connect(self._show_lookups)
            self.lookups.finished.connect(self._lookups_finished)
            self.lookups.message.connect(self.statusBar().showMessage)
            self.lookups.consent_changed.connect(self._online_consent_changed)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setMaximumWidth(120)
        self.busy.setVisible(False)
        self.statusBar().addPermanentWidget(self.busy)
        self.problem_badge = QToolButton()
        self.problem_badge.setObjectName("problem_badge")
        self.problem_badge.setAutoRaise(True)
        self.problem_badge.setToolTip("Problems with files this session: show the activity panel")
        self.problem_badge.clicked.connect(self.show_activity)
        self.problem_badge.setVisible(False)
        self.statusBar().addPermanentWidget(self.problem_badge)
        self._problems_seen = -1
        self._problem_timer = QTimer(self)
        self._problem_timer.setInterval(1000)
        self._problem_timer.timeout.connect(self._check_problems)
        self._problem_timer.start()
        self.statusBar().showMessage("Ready")
        self.scans.started.connect(self._scan_started)
        self.scans.progress.connect(self.statusBar().showMessage)
        self.scans.progress.connect(self.activity.set_now)
        self.scans.finished.connect(self._scan_finished)
        self.scans.failed.connect(self._scan_failed)

        self._load_saved_searches()
        self.refresh_keywords()
        self.navigation.select(OPENING_PAGE)  # the keep at a glance (DESIGN.md §12)

    # --- navigation ---

    def show_target(self, target: NavTarget, *, record: bool = True) -> None:
        """Show the page for ``target``, creating it on first use. ``record`` adds it to
        the back/forward history (going back and forward doesn't)."""
        page = self._pages.pop(target, None)
        if page is None:
            page = self._make_page(target)
            self.stack.addWidget(page)
        self._pages[target] = page  # most recently shown last
        self.stack.setCurrentWidget(page)
        if record:
            self._record(target)
        self.navigation.show_current(target)
        self._forget_old_details()
        self._schedule_summary()

    # --- back and forward ---

    def back(self) -> None:
        if self._history_index > 0:
            self._history_index -= 1
            self.show_target(self._history[self._history_index], record=False)
            self._show_history()

    def forward(self) -> None:
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.show_target(self._history[self._history_index], record=False)
            self._show_history()

    def _record(self, target: NavTarget) -> None:
        index = self._history_index
        if 0 <= index < len(self._history) and self._history[index] == target:
            return  # showing the current page again
        del self._history[self._history_index + 1 :]  # a new path drops the forward pages
        self._history.append(target)
        del self._history[:-MAX_HISTORY]
        self._history_index = len(self._history) - 1
        self._show_history()

    def _show_history(self) -> None:
        self.back_action.setEnabled(self._history_index > 0)
        self.forward_action.setEnabled(self._history_index < len(self._history) - 1)

    def _forget_old_details(self) -> None:
        """Keep the most recently shown detail pages; older ones are made again if the
        user goes back to them."""
        details = [t for t, p in self._pages.items() if isinstance(p, DetailPage)]
        for target in details[:-MAX_DETAIL_PAGES]:
            page = self._pages.pop(target)
            self.stack.removeWidget(page)
            page.deleteLater()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """The mouse's back and forward buttons, anywhere in this window."""
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and isinstance(event, QMouseEvent)
            and isinstance(watched, QWidget)
            and watched.window() is self
        ):
            if event.button() == Qt.MouseButton.BackButton:
                self.back()
                return True
            if event.button() == Qt.MouseButton.ForwardButton:
                self.forward()
                return True
        return super().eventFilter(watched, event)

    def _make_page(self, target: NavTarget) -> QWidget:
        session = self.session
        assert session is not None
        if target.kind == "saved" and int(target.key) in self._saved:
            name, data = self._saved[int(target.key)]
            try:
                saved = SavedDefinition.from_json(data)
            except SavedSearchError as e:
                return QLabel(f"{name}\n\n{e}")
            page = self._search_page(
                name,
                saved.base,
                f"saved:{target.key}",
                grouped=saved.grouped,
                layout=saved.layout,
            )
            page.restore(saved)
            return page
        if target.kind in ("search", "view"):
            layout = "list"
            if target.kind == "search":
                spec = SearchSpec()
            else:
                views = [v for v in session.theme.views if isinstance(v, SearchView)]
                view = next(v for v in views if v.name == target.key)
                spec = view_spec(session.schema, view)
                layout = view.layout
            return self._search_page(
                target.label,
                spec,
                f"{target.kind}:{target.key}",
                grouped=target.kind == "search",
                layout=layout,
            )
        if target.kind == "entity":
            detail = DetailPage(
                session,
                int(target.key),
                thumbnails=self.thumbnails,
                make_contents=self._contents_search,
            )
            detail.open_entity.connect(self.open_entity)
            detail.field_edited.connect(self.tag_actions.edit_field)
            detail.extra_edited.connect(self.tag_actions.edit_extra)
            detail.reread_requested.connect(self.reread)
            detail.write_back_requested.connect(self.write_back)
            detail.look_up_requested.connect(self.look_up_online)
            detail.unlink_requested.connect(self.tag_actions.unlink_file)
            detail.keyword_map_requested.connect(lambda info: self.map_keywords([info]))
            detail.keyword_ignore_requested.connect(self.tag_actions.ignore_keywords)
            detail.file_tag_restore_requested.connect(self.restore_file_tag)
            detail.relate_requested.connect(self.add_related)
            detail.unrelate_requested.connect(self.tag_actions.remove_related)
            detail.move_requested.connect(self.tag_actions.move_related)
            detail.uncontain_requested.connect(self.tag_actions.uncontain)
            detail.action_requested.connect(self.run_action)
            detail.file_opener = self.files
            detail.show_in_search.connect(lambda _id: self._contents_in_search(detail))
            detail.selection_changed.connect(self._schedule_summary)
            return detail
        if target.kind == "dashboard":
            dashboard = DashboardPage(session, self.thumbnails)
            dashboard.open_type.connect(self._open_type)
            dashboard.open_triage.connect(self._open_triage)
            dashboard.configure_root.connect(
                lambda root_id: self.configure_keep().select_root(root_id)
            )
            dashboard.open_entity.connect(self.open_entity)
            dashboard.search_tag.connect(lambda tag_id: self.add_tags_to_search([tag_id]))
            dashboard.search_value.connect(self._search_value)
            return dashboard
        if target.kind == "dedupe":
            dedupe = DedupePage(session, self.files, thumbnails=self.thumbnails)
            dedupe.open_entity.connect(self.open_entity)
            dedupe.merge_requested.connect(self.merge_items)
            dedupe.versions_requested.connect(
                lambda ids: self.tag_actions.merge_items(ids[0], ids[1:], {})
            )
            dedupe.not_duplicate_requested.connect(self.tag_actions.set_not_duplicate)
            return dedupe
        if target.kind == "triage":
            triage = TriagePage(
                session,
                lambda title, spec, key: self._search_page(title, spec, key),
                self.files,
            )
            triage.dismiss_requested.connect(self.tag_actions.dismiss)
            triage.delete_requested.connect(self.delete_items)
            triage.skip_requested.connect(self._skip_files)
            triage.link_requested.connect(self.link_files)
            triage.message.connect(lambda text: self.statusBar().showMessage(text, 8000))
            triage.keywords_requested.connect(self.review_keywords)
            for listed in (triage.untagged, triage.missing, triage.keywords):
                listed.selection_changed.connect(self._schedule_summary)
            triage.tabs.currentChanged.connect(lambda _: self._schedule_summary())
            return triage
        if target.kind == "keywords":
            keywords = KeywordsPage(session)
            keywords.map_requested.connect(self.map_keywords)
            keywords.create_requested.connect(self.create_keyword_tag)
            keywords.ignore_requested.connect(self.tag_actions.ignore_keywords)
            return keywords
        if target.kind == "tags":
            manager = TagManagerPage(session)
            manager.add_requested.connect(self.tag_actions.add_tag)
            manager.rename_requested.connect(self.tag_actions.rename)
            manager.move_requested.connect(self._move_tag)
            manager.merge_requested.connect(self._merge_tags)
            manager.delete_requested.connect(self._delete_tag)
            details = manager.details
            details.color_chosen.connect(self._set_tag_color)
            details.description_saved.connect(self._set_tag_description)
            details.alias_added.connect(self._add_tag_alias)
            details.alias_removed.connect(self._remove_tag_alias)
            details.types_requested.connect(self.change_tag_types)
            manager.undo_requested.connect(self.tag_actions.undo)
            manager.redo_requested.connect(self.tag_actions.redo)
            manager.set_history(self.tag_actions.undo_label, self.tag_actions.redo_label)
            return manager
        coming = QLabel(f"{target.label}\n\n{_COMING[target.kind]}")
        coming.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return coming

    def _search_page(
        self,
        title: str,
        spec: SearchSpec,
        state_key: str,
        *,
        grouped: bool = False,
        layout: str = "list",
    ) -> SearchPage:
        """A search page wired to the window. What the user hides, the layout, and the card
        lines are remembered under ``state_key`` in ui_state.json."""
        session = self.session
        assert session is not None
        hidden = self._ui_state.get("hidden_columns", {}).get(state_key)
        if hidden is None and state_key == "triage:keywords":
            hidden = sorted(DEFAULT_HIDDEN - {KEYWORDS})  # the keywords are the point

        shown = self._ui_state.get("shown_columns", {}).get(state_key)
        layout = self._ui_state.get("layouts", {}).get(state_key, layout)
        card_lines = self._ui_state.get("card_lines", {}).get(state_key)
        toggles = self._ui_state.get("toggles", {}).get(state_key)
        search = SearchPage(
            session,
            title,
            spec,
            grouped=grouped,
            hidden_columns=hidden,
            shown_columns=shown if isinstance(shown, list) else (),
            layout_mode=layout,
            card_lines=card_lines if isinstance(card_lines, list) else None,
            thumbnails=self.thumbnails,
            thumbnail_size=self.thumbnail_size,
            preview=self.preview_action.isChecked(),
            toggles=toggles if isinstance(toggles, dict) else None,
        )
        search.size_menu = self.size_menu
        search.open_requested.connect(self.open_entity)
        search.save_requested.connect(lambda: self.save_search(as_new=False, page=search))
        search.reread_requested.connect(self.reread)
        search.write_back_requested.connect(self.write_back)
        search.look_up_requested.connect(self.look_up_online)
        search.new_item_requested.connect(self.new_container)
        search.add_to_requested.connect(self.add_to_container)
        search.action_requested.connect(self.run_action)
        search.file_opener = self.files
        search.zoom_requested.connect(self.zoom)
        search.layout_changed.connect(
            lambda mode: self._save_view_state("layouts", state_key, mode)
        )
        search.card_lines_changed.connect(
            lambda names: self._save_view_state("card_lines", state_key, names)
        )
        search.toggles_changed.connect(
            lambda toggles: self._save_view_state("toggles", state_key, toggles)
        )
        search.tags_dropped.connect(self.apply_tags)
        search.selection_changed.connect(self._schedule_summary)
        search.hidden_columns_changed.connect(
            lambda keys: self._save_hidden_columns(state_key, keys)
        )
        search.shown_columns_changed.connect(
            lambda keys: self._save_view_state("shown_columns", state_key, keys)
        )
        return search

    def _preview_toggled(self, visible: bool) -> None:
        """Show or hide the preview strip on every search page, and remember it."""
        assert self.session is not None
        for page in self.search_pages():
            page.set_preview_visible(visible)
        self._ui_state["preview"] = visible
        save_ui_state(self.session.keep.ui_state_path, self._ui_state)

    def _contents_search(self, key: str, spec: SearchSpec, title: str) -> SearchPage:
        """A search embedded in a detail page: a container's contents, or a related section
        (#125). Those with the same ``key`` (``contents:<type>``, ``related:<type>``) share
        their remembered layout and columns."""
        return self._search_page(title, spec, key, layout="grid")

    def _contents_in_search(self, detail: DetailPage) -> None:
        """Search all, listing only a detail page's contents (a Within chip)."""
        if detail.detail is None:
            return
        self.navigation.select(NavTarget("search", label="Search all"))
        page = self.stack.currentWidget()
        assert isinstance(page, SearchPage)
        page.show_within(detail.entity_id, detail.detail.title, detail.detail.type)

    def reread(self, entity_ids: list[int], replace_edits: bool) -> None:
        """Read items' files again (asking first when that replaces the user's edits)."""
        if replace_edits and not self.confirm_replace_edits(len(entity_ids)):
            return
        self.statusBar().showMessage("Reading files again\u2026")
        self.tag_actions.reread(entity_ids, replace_edits)

    # --- Write to file… (#299) ---

    def write_back(self, entity_ids: list[int]) -> None:
        """Work out what Write to file… would change (in a worker), show it, and write the
        files the user keeps ticked; then scan them, so Tagalot reads what it wrote."""
        session = self.session
        assert session is not None
        roots = [r for r in session.keep.config.roots if r.watched]
        paths = {r.id: session.root_path(r.id) for r in roots}
        writable = {r.id for r in roots if r.writable}

        def plan() -> WritePlan:
            tree = session.tag_cache.get()
            with session.reader.connect() as conn:
                return plan_write_back(conn, session.schema, tree, entity_ids, paths, writable)

        def planned(found: WritePlan) -> None:
            if not shiboken6.isValid(self):
                return
            self.statusBar().clearMessage()
            if not any(f.changes for f in found.files):
                self._nothing_to_write(found)
                return
            chosen = self.confirm_write_back(found)
            if chosen:
                self.statusBar().showMessage("Writing files\u2026")
                run_in_pool(
                    lambda: write_files(chosen, session.keep.dir, writable),
                    on_done=self._written,
                    on_error=lambda e: self.statusBar().showMessage(f"Couldn't write: {e}"),
                )

        self.statusBar().showMessage("Reading files to write\u2026")
        run_in_pool(
            plan,
            on_done=planned,
            on_error=lambda e: self.statusBar().showMessage(f"Couldn't read the files: {e}"),
        )

    def confirm_write_back(self, plan: WritePlan) -> list[FileWrite]:
        """Show the preview; the files to write (none if the user cancels)."""
        dialog = WriteBackDialog(plan, self)
        return dialog.chosen() if dialog.exec() == QDialog.DialogCode.Accepted else []

    def _nothing_to_write(self, plan: WritePlan) -> None:
        reasons = [f"{f.title} ({f.relpath}): {f.problem}" for f in plan.files if f.problem]
        reasons += [f"{title}: {why}" for title, why in plan.skipped]
        if not reasons:
            self.statusBar().showMessage("Nothing to write: the files already say this.")
            return
        QMessageBox.information(
            self, "Nothing to write", "Nothing was written.\n\n" + "\n".join(reasons)
        )

    def _written(self, results: list[WriteResult]) -> None:
        if not shiboken6.isValid(self) or self.session is None:
            return
        done = [r for r in results if r.error is None]
        failed = [r for r in results if r.error is not None]
        count = f"{len(done)} file" + ("" if len(done) == 1 else "s")
        text = f"Wrote {count}."
        if done and done[0].backup is not None:
            folder = done[0].backup.relative_to(self.session.keep.dir).parts[:2]
            text += f" Copies of the originals are in {'/'.join(folder)} in the keep folder."
        self.statusBar().showMessage(text)
        if failed:
            QMessageBox.warning(
                self,
                "Some files weren't written",
                "\n".join(f"{r.write.relpath}: {r.error}" for r in failed),
            )
        if done:
            self._scan_roots(sorted({r.write.root_id for r in done}))

    def run_action(self, method: str, entity_ids: list[int]) -> None:
        """Run a theme action (a menu entry or a page's button), then open what it asks."""
        assert self.session is not None
        spec = self.session.theme.actions().get(method)
        self.statusBar().showMessage(f"{spec.label if spec else method}\u2026")
        self.tag_actions.run_action(method, entity_ids, self._action_outputs)

    def _action_outputs(self, result: ActionResult) -> None:
        for output in result.outputs:
            kind, value = output[0], output[1]
            if kind in ("open", "reveal"):
                self.files.act(FileToOpen(0, value, False), OPEN if kind == "open" else REVEAL)
            elif kind == "copy":
                QApplication.clipboard().setText(value)
            elif kind == "url":
                open_web_address(value)
            elif kind == "save" and len(output) == 3:
                self.save_export(value, output[2], result.change.label)

    # --- saving an action's export (#322) ---

    def save_export(self, name: str, text: str, by: str = "an action") -> None:
        """Ask where to save an action's export, then write it (in the background). A place
        inside a watched folder is allowed after a warning, and the file is then skipped by
        scans (an exact pattern on that folder's Skip list), so the keep doesn't read its
        own export back; an existing file there is never replaced."""
        session = self.session
        assert session is not None
        path = self.choose_export_path(name)
        if not path:
            return
        roots = {r.id: session.root_path(r.id) for r in session.keep.config.roots}
        place = place_in_roots(path, roots)
        if place is not None:
            root = next(r for r in session.keep.config.roots if r.id == place[0])
            if os.path.exists(path):
                QMessageBox.warning(
                    self,
                    "Not saved",
                    f"{place[1]} is already in {root.name}, a folder this keep watches. "
                    "Tagalot never replaces files there: choose a new name.",
                )
                return
            if not self.confirm_export_in_root(root.name, place[1]):
                return

        def job() -> str:
            if place is not None:  # skipped first, so no scan can read it in between
                note = (
                    f"Saved here by {by} on {datetime.now():%Y-%m-%d}; skipped so the keep "
                    "doesn't read its own export back"
                )
                config = add_skipped(
                    session.keep.config, session.keep.dir, place[0], [exact_pattern(place[1])], note
                )
                session.save_config(config)
            Path(path).write_text(text, encoding="utf-8")
            return path

        def done(saved: str) -> None:
            if not shiboken6.isValid(self):
                return
            note = (
                " It is in a watched folder, so scans skip it (Keep configuration \u2192 "
                "Folders \u2192 Skip)."
                if place is not None
                else ""
            )
            self.statusBar().showMessage(f"Saved {Path(saved).name}.{note}")
            if place is not None and self.keep_config is not None:
                self.keep_config.reload()

        run_in_pool(
            job,
            on_done=done,
            on_error=lambda e: self.statusBar().showMessage(f"Couldn't save {name}: {e}"),
        )

    def choose_export_path(self, name: str) -> str:
        """Ask where to save an export (tests replace this)."""
        assert self.session is not None
        return choose_save_file(self, "Save", name, self.session.settings)

    def confirm_export_in_root(self, root_name: str, relpath: str) -> bool:
        """Ask before saving inside a watched folder (tests replace this)."""
        answer = QMessageBox.question(
            self,
            "Save in a watched folder?",
            f"{relpath} is in {root_name}, a folder this keep watches.\n\n"
            "Tagalot will leave it out of scans (it is added to the folder's Skip list in "
            "Keep configuration), so the keep doesn't read its own export back. Save here?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Save

    def confirm_replace_edits(self, count: int) -> bool:
        """Ask before the files' values replace what the user edited (tests replace this)."""
        items = "this item" if count == 1 else f"these {count:,} items"
        answer = QMessageBox.question(
            self,
            "Replace your edits",
            f"Replace what you edited on {items} with what the files say?\n\n"
            "Edit \u2192 Undo can put your edits back.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def open_entity(self, entity_id: int) -> None:
        """Show an entity's detail page (double-click, Enter, or a link on another page)."""
        self.show_target(NavTarget("entity", key=str(entity_id)))

    def current_items_page(self) -> SearchPage | DetailPage | None:
        """The current page if it has items to tag: a search, or a detail page."""
        page = self.stack.currentWidget()
        if isinstance(page, TriagePage):
            return page.current_search()  # the Untagged or Missing tab's results
        return page if isinstance(page, SearchPage | DetailPage) else None

    # --- tagging ---

    def apply_tags(self, entity_ids: list[int], tag_ids: list[int]) -> None:
        """Tag entities (a drop on results), in the background; undoable."""
        self.tag_actions.apply(entity_ids, tag_ids, self.tag_names(tag_ids))

    def tag_selection(self, tag_ids: list[int], remove: bool = False) -> None:
        """Apply (or remove) tags on the current page's selected items (Enter or
        Shift+Enter in the Tags panel)."""
        page = self.current_items_page()
        if page is None:
            self.statusBar().showMessage("Open a search to tag its items.")
            return
        names = self.tag_names(tag_ids)

        def selected(ids: list[int]) -> None:
            if not ids:
                self.statusBar().showMessage(f"Select items to tag with {names} first.")
            elif remove:
                self.tag_actions.remove(ids, tag_ids, names)
            else:
                self.tag_actions.apply(ids, tag_ids, names)

        page.selected_entity_ids(selected)

    def create_tag(self, text: str) -> None:
        """Create the tag typed in the Tags panel (a path like "Places > Norway" creates
        missing parents too) and apply it to the current page's selected items, if any."""
        try:
            names = split_tag_path(text)
        except TagError as e:
            self.statusBar().showMessage(str(e))
            return
        path = repr(PATH_SEPARATOR.join(names))
        page = self.current_items_page()
        if page is not None:
            page.selected_entity_ids(lambda ids: self.tag_actions.create(names, ids, path))
        else:
            self.tag_actions.create(names, [], path)

    def add_tags_to_search(self, tag_ids: list[int], exclude: bool = False) -> None:
        """Add tags to the current search's filter bar (Search all if the current page
        isn't a search), as include chips or "but not" chips."""
        page = self.stack.currentWidget()
        if not isinstance(page, SearchPage):
            self.navigation.select(NavTarget("search", label="Search all"))
            page = self.stack.currentWidget()
        assert isinstance(page, SearchPage)
        for tag_id in tag_ids:
            page.filter_bar.add_tag(tag_id, exclude=exclude)
        how = "Hiding items with" if exclude else "Showing only items with"
        self.statusBar().showMessage(f"{how} {self.tag_names(tag_ids)}.")

    def _move_tag(self, tag_id: int, parent_id: int | None) -> None:
        tree = self.tag_panel.model.tree
        if tree is None or tag_id not in tree:
            return
        target = tree.node(parent_id).name if parent_id is not None and parent_id in tree else None
        self.tag_actions.move(tag_id, parent_id, tree.node(tag_id).name, target)

    def _merge_tags(self, source_id: int, target_id: int) -> None:
        tree = self.tag_panel.model.tree
        if tree is None or source_id not in tree or target_id not in tree:
            return
        source, target = tree.node(source_id).name, tree.node(target_id).name
        self.tag_actions.merge(source_id, target_id, source, target)

    def _delete_tag(self, tag_id: int, mode: DeleteMode | None) -> None:
        tree = self.tag_panel.model.tree
        if tree is not None and tag_id in tree:
            self.tag_actions.delete(tag_id, mode, tree.node(tag_id).name)

    # --- the tag manager's details pane (§7) ---

    def _tag_name(self, tag_id: int) -> str:
        tree = self.tag_panel.model.tree
        return tree.node(tag_id).name if tree is not None and tag_id in tree else "the tag"

    def _set_tag_color(self, tag_id: int, color: str | None) -> None:
        name = self._tag_name(tag_id)
        tags = self.tag_actions.session.tags
        message = (
            f"Set the color of {name!r} to {color}." if color else f"Cleared the color of {name!r}."
        )
        self.tag_actions.change(lambda: tags.set_color(tag_id, color), message)

    def _set_tag_description(self, tag_id: int, description: str) -> None:
        name = self._tag_name(tag_id)
        tags = self.tag_actions.session.tags
        message = (
            f"Saved the description of {name!r}."
            if description.strip()
            else f"Cleared the description of {name!r}."
        )
        self.tag_actions.change(lambda: tags.set_description(tag_id, description), message)

    def change_tag_types(self, tag_id: int) -> None:
        """Choose which item types a tag applies to; if that takes it off some items, ask
        first (#135)."""
        session = self.session
        if session is None:
            return
        tree = session.tag_cache.get()
        if tag_id not in tree:
            return
        node = tree.node(tag_id)
        inherited = tree.scope(node.parent_id) if node.parent_id is not None else None
        plurals = type_plurals(session.schema)
        chosen = self.choose_tag_types(
            TagTypesDialog(node.name, plurals, parse_types(node.types), inherited, self)
        )
        if chosen is False:
            return
        types = chosen if isinstance(chosen, frozenset) else None

        def count() -> int:
            with session.reader.connect() as conn:
                return len(scope_conflicts(conn, tree.with_types(tag_id, types), tag_id))

        def counted(conflicts: int) -> None:
            if not shiboken6.isValid(self):
                return
            if conflicts and not self.confirm_tag_type_removal(node.name, conflicts):
                return
            if types is None:
                message = f"{node.name!r} now applies to every type."
            else:
                names = sorted(plurals.get(t, t) for t in types)
                message = f"{node.name!r} now applies to {', '.join(names)}."
            if conflicts:
                message += f" Removed it from {conflicts:,} item{'s' if conflicts != 1 else ''}."
            tags = session.tags
            self.tag_actions.change(lambda: tags.set_types(tag_id, types), message)

        run_in_pool(count, on_done=counted)

    def choose_tag_types(self, dialog: TagTypesDialog) -> frozenset[str] | bool | None:
        """The types chosen (``None``: every type), or ``False`` if cancelled (tests replace
        this)."""
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        return dialog.chosen()

    def confirm_tag_type_removal(self, name: str, count: int) -> bool:
        """Ask before taking a tag off items its new types don't allow (tests replace this)."""
        items = (
            "1 item of another type has" if count == 1 else f"{count:,} items of other types have"
        )
        answer = QMessageBox.question(
            self,
            "Remove the tag from other types?",
            f"{items} {name!r} or one of its sub-tags. Remove it from "
            f"{'that item' if count == 1 else 'those items'}? (You can undo this.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    # --- file keywords (#295) ---

    def map_keywords(self, keywords: list[KeywordInfo]) -> None:
        """Ask for a tag and tie these keywords to it (as its aliases)."""
        session = self.session
        if session is None or not keywords:
            return
        words = [k.keyword for k in keywords]
        label = words[0] if len(words) == 1 else f"{len(words)} keywords"
        tag_id = self.choose_keyword_tag(map_dialog(session.tag_cache.get(), label, self))
        if tag_id is None:
            return
        tags = session.tags
        path = PATH_SEPARATOR.join(session.tag_cache.get().path(tag_id))
        self.tag_actions.change(
            lambda: tags.map_keywords(tag_id, words), f"Mapped {label!r} to {path}."
        )

    def choose_keyword_tag(self, dialog: TagPickerDialog) -> int | None:
        """The tag picked, or ``None`` if cancelled (tests replace this)."""
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.target()

    def create_keyword_tag(self, keyword: KeywordInfo) -> None:
        """Make a tag for a keyword (at a path the user confirms), then tie the keyword to it."""
        session = self.session
        if session is None:
            return
        default = " > ".join(p.strip() for p in keyword.keyword.split("/") if p.strip())
        text = self.ask_keyword_tag_path(keyword.keyword, default)
        if not text:
            return
        try:
            names = split_tag_path(text)
        except TagError as e:
            self.statusBar().showMessage(str(e))
            return
        tags = session.tags

        def work() -> None:
            tag_id = tags.add_path(names)
            # Tie the keyword to the tag only if it doesn't already match it by path or name.
            matched = keyword_index(session.tag_cache.get()).get(keyword_key(keyword.keyword))
            if matched != tag_id:
                tags.map_keywords(tag_id, [keyword.keyword])

        path = PATH_SEPARATOR.join(names)
        self.tag_actions.change(work, f"Created tag {path} for {keyword.keyword!r}.")

    def ask_keyword_tag_path(self, keyword: str, default: str) -> str | None:
        """The path for a keyword's new tag, or ``None`` if cancelled (tests replace this)."""
        text, ok = QInputDialog.getText(
            self,
            "Create tag",
            f"A tag for \u201c{keyword}\u201d (a path such as Genre > Fantasy):",
            text=default,
        )
        return text.strip() if ok and text.strip() else None

    def restore_file_tag(self, entity_id: int, tag_id: int) -> None:
        """Put back a tag an item's file gives, which the user had removed (#295)."""
        session = self.session
        if session is None:
            return
        name = self._tag_name(tag_id)
        tags = session.tags
        self.tag_actions.change(
            lambda: tags.restore_file_tag(entity_id, tag_id),
            f"Restored {name!r}: the item's file gives it again.",
        )

    def refresh_keywords(self, *, after_scan: bool = False) -> None:
        """Count the unmatched keywords for TOOLS (and, after a scan, say how many are
        new), and reload the File keywords page if it's open."""
        session = self.session
        if session is None:
            return
        for page in self._pages.values():
            if isinstance(page, KeywordsPage):
                page.refresh()

        def count() -> tuple[frozenset[str], bool]:
            tree = session.tag_cache.get()
            with session.reader.connect() as conn:
                return unmatched_keys(conn, tree), any_keywords(conn)

        def counted(found: tuple[frozenset[str], bool]) -> None:
            if not shiboken6.isValid(self):
                return
            keys, present = found
            if present != session.has_keywords:  # lists gain or lose the column
                session.has_keywords = present
                for page in self.search_pages():
                    page.refresh()
            new = keys - self._unmatched_keywords if after_scan else frozenset()
            self._unmatched_keywords = keys
            self.navigation.set_count("keywords", len(keys))
            if after_scan:
                self.activity.set_new_keywords(len(new))
                if new:
                    words = "keyword doesn't" if len(new) == 1 else "keywords don't"
                    self.statusBar().showMessage(
                        f"{self.statusBar().currentMessage()} {len(new):,} new file "
                        f"{words} match a tag."
                    )

        run_in_pool(count, on_done=counted)

    def review_keywords(self) -> None:
        self.navigation.select(NavTarget("keywords", label="File keywords"))
        page = self.stack.currentWidget()
        if isinstance(page, KeywordsPage):
            page.show_box.setCurrentText("Unmatched")

    def _add_tag_alias(self, tag_id: int, alias: str) -> None:
        name = self._tag_name(tag_id)
        tags = self.tag_actions.session.tags
        self.tag_actions.change(
            lambda: tags.add_alias(tag_id, alias), f"{name!r} is now also called {alias!r}."
        )

    def _remove_tag_alias(self, tag_id: int, alias: str) -> None:
        name = self._tag_name(tag_id)
        tags = self.tag_actions.session.tags
        self.tag_actions.change(
            lambda: tags.remove_alias(tag_id, alias), f"{name!r} is no longer called {alias!r}."
        )

    def focus_tag_filter(self) -> None:
        """Show the Tags panel and put the cursor in its filter box (Ctrl+T)."""
        self.tags_dock.show()
        self.tags_dock.raise_()
        self.tag_panel.focus_filter()

    def tag_names(self, tag_ids: list[int]) -> str:
        """How tags read in a message: 'Iceland', 'Iceland' and 'Beach', or 4 tags."""
        tree = self.tag_panel.model.tree
        names = [
            repr(tree.display_name(t)) if tree is not None and t in tree else "a tag"
            for t in tag_ids
        ]
        if len(names) > 3:
            return f"{len(names)} tags"
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]

    def _schedule_summary(self) -> None:
        if self.session is not None:
            self._summary_timer.start()

    def update_selection_summary(self) -> None:
        """Count, in a worker, which tags the current page's selected items carry, and
        show it in the Tags panel."""
        session = self.session
        page = self.current_items_page()
        self._summary_generation += 1
        generation = self._summary_generation
        if session is None or page is None:
            self.tag_panel.set_selection(0, {})
            return

        def show(
            count: int,
            counts: dict[int, int],
            types: frozenset[str],
            from_files: dict[int, int] | None = None,
        ) -> None:
            if generation == self._summary_generation and shiboken6.isValid(self.tag_panel):
                self.tag_panel.set_selection(count, counts, types, from_files)

        def selected(ids: list[int]) -> None:
            if not ids:
                show(0, {}, frozenset())
                return

            def count() -> tuple[dict[int, int], frozenset[str], dict[int, int]]:
                with session.reader.connect() as conn:
                    counts, types = tag_counts(conn, ids), entity_types(conn, ids)
                    return counts, types, file_tag_counts(conn, ids)

            run_in_pool(count, on_done=lambda found: show(len(ids), *found))

        page.selected_entity_ids(selected)

    def _tags_changed(self, message: str) -> None:
        self.statusBar().showMessage(message)
        self._refresh_details()  # a field edit (or its undo) shows on the page
        self._schedule_summary()
        for page in self.search_pages():
            page.refresh()  # tag filters may now match differently
        self.tag_panel.reload()  # undo can change the tag tree
        for manager in self._pages.values():
            if isinstance(manager, TagManagerPage):
                manager.reload()  # usage counts, and the tree after an undo
        self._update_undo_actions()
        self._refresh_triage()
        self.refresh_keywords()  # a mapping, an alias, or a rename changes what matches
        self._load_saved_searches()  # saving, renaming, and their undo

    def _tag_message(self, message: str) -> None:
        self.statusBar().showMessage(message)
        self._update_undo_actions()
        self._refresh_details()  # a refused edit: show the stored value again

    def _update_undo_actions(self) -> None:
        for action, verb, label in (
            (self.undo_action, "Undo", self.tag_actions.undo_label),
            (self.redo_action, "Redo", self.tag_actions.redo_label),
        ):
            action.setEnabled(label is not None)
            action.setText(f"{verb} {label}" if label else verb)
        for page in self._pages.values():
            if isinstance(page, TagManagerPage):
                page.set_history(self.tag_actions.undo_label, self.tag_actions.redo_label)

    def _refresh_details(self) -> None:
        for page in self._pages.values():
            if isinstance(page, DetailPage):
                page.refresh()

    def search_pages(self) -> list[SearchPage]:
        """The search pages created so far, including those inside detail pages."""
        pages = []
        for page in self._pages.values():
            if isinstance(page, SearchPage):
                pages.append(page)
            elif isinstance(page, DetailPage):
                pages += page.embedded()
            elif isinstance(page, TriagePage):
                pages += [page.untagged, page.missing, page.keywords]
        return pages

    def _refresh_triage(self) -> None:
        """Recount the pages that summarize the keep (Triage, the dashboard)."""
        for page in self._pages.values():
            if isinstance(page, TriagePage | DashboardPage | DedupePage):
                page.refresh()

    def _open_type(self, type_id: str) -> None:
        """Search all, narrowed to one type (from the dashboard)."""
        self.navigation.select(NavTarget("search", label="Search all"))
        page = self.stack.currentWidget()
        if isinstance(page, SearchPage):
            page.show_all(type_id)

    def _search_value(self, type_id: str, value_filter: FieldFilter) -> None:
        """Search all, narrowed to one type's items with a value (a dashboard card)."""
        self._open_type(type_id)
        page = self.stack.currentWidget()
        if isinstance(page, SearchPage):
            page.filter_bar.set_field_filter(value_filter.field, value_filter)

    def _open_triage(self) -> None:
        self.navigation.select(NavTarget("triage", label="Triage"))
        page = self.stack.currentWidget()
        if isinstance(page, TriagePage):
            page.tabs.setCurrentIndex(UNTAGGED_TAB)

    def delete_items(self, ids: list[int]) -> None:
        """Delete items from the triage list, after asking; Edit → Undo brings them back."""
        if ids and self.confirm_delete_items(len(ids)):
            self.tag_actions.delete_items(ids)

    def confirm_delete_items(self, count: int) -> bool:
        """Ask before deleting items (tests replace this)."""
        items = "this item" if count == 1 else f"these {count:,} items"
        answer = QMessageBox.question(
            self,
            "Delete items",
            f"Delete {items} and their tags from this keep? No files are changed, and "
            "Edit \u2192 Undo brings them back.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def link_files(self, files: list[UnlinkedFile]) -> None:
        """Ask which item (and role) to link files to, then link them by hand."""
        session = self.session
        assert session is not None
        what = files[0].relpath.rpartition("/")[2] if len(files) == 1 else f"{len(files)} files"
        kinds = [kind_of_file(ResourceKind.FILE, _ext(f.relpath)) for f in files]
        target = self.choose_link_target(what, kinds)
        if target is not None:
            entity_id, role = target
            self.tag_actions.link_files(entity_id, [f.resource_id for f in files], role)

    def merge_items(self, items: list[tuple[int, str, str]]) -> None:
        """Ask which item to keep (and which values win), then merge; one undo step."""
        chosen = self.choose_merge(items)
        if chosen is not None:
            keep, others, choices = chosen
            self.tag_actions.merge_items(keep, others, choices)

    def choose_merge(
        self, items: list[tuple[int, str, str]]
    ) -> tuple[int, list[int], dict[str, int]] | None:
        """The Merge dialog: (kept id, the others, conflict choices), or ``None`` (tests
        replace this)."""
        assert self.session is not None
        dialog = MergeDialog(self.session, items, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.keep_id, dialog.other_ids, dict(dialog.choices)

    def add_related(self, entity_id: int, name: str, side: str = "") -> None:
        """Ask which item to add to a related section (or a new one's name), then add it.
        ``side`` picks the section of a relationship of a type with itself (Cites, or
        Cited by)."""
        page = self.stack.currentWidget()
        detail = page.detail if isinstance(page, DetailPage) else None
        section = next(
            (
                s
                for s in (detail.sections if detail is not None else ())
                if s.kind == "related" and s.relationship == name and (s.side or "") == side
            ),
            None,
        )
        if detail is None or section is None or section.other_type is None:
            return
        chosen = self.choose_related(
            detail.title,
            section.other_type,
            section.title,
            {e.id for e in section.entities}
            | ({entity_id} if section.other_type == detail.type else set()),  # not itself
        )
        if chosen is not None:
            other_id, new_title = chosen
            self.tag_actions.add_related(entity_id, name, other_id, new_title, side or None)

    # --- projects: containers made by hand (#329) ---

    def new_container(self, type_id: str) -> None:
        """New project…: ask its name, make it, and open its page."""
        assert self.session is not None
        noun = entity_label(self.session.schema.by_type_id(type_id).entity).lower()
        name = self.ask_new_name(noun)
        if name:
            self.tag_actions.new_container(type_id, name, then=self.open_entity)

    def add_to_container(self, type_id: str, entity_ids: list[int]) -> None:
        """Add to project…: choose one (or name a new one), then put the items in it."""
        chosen = self.choose_container(type_id, len(entity_ids))
        if chosen is None:
            return
        container_id, new_name = chosen
        if container_id is not None:
            self.tag_actions.contain(container_id, entity_ids)
        elif new_name:
            self.tag_actions.new_container(
                type_id, new_name, then=lambda new: self.tag_actions.contain(new, entity_ids)
            )

    def ask_new_name(self, noun: str) -> str:
        """Ask a new item's name (tests replace this)."""
        name, ok = QInputDialog.getText(self, f"New {noun}", f"Name of the new {noun}:")
        return name.strip() if ok else ""

    def choose_container(self, type_id: str, count: int) -> tuple[int | None, str | None] | None:
        """The Add to … dialog: (an existing item's id, None), (None, a new one's name), or
        ``None`` (tests replace this)."""
        assert self.session is not None
        noun = entity_label(self.session.schema.by_type_id(type_id).entity).lower()
        what = "this item" if count == 1 else f"these {count:,} items"
        dialog = RelateDialog(
            self.session, what, type_id, noun, set(), self, prompt=f"Add {what} to a {noun}:"
        )
        dialog.setWindowTitle(f"Add to {noun}")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.chosen()

    def choose_related(
        self, title: str, other_type: str, section: str, already: set[int]
    ) -> tuple[int | None, str | None] | None:
        """The Add to… dialog: (item id, None), (None, a new item's name), or ``None``
        (tests replace this)."""
        assert self.session is not None
        dialog = RelateDialog(self.session, title, other_type, section, already, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.chosen()

    def choose_link_target(self, what: str, kinds: list[Kind | None]) -> tuple[int, str] | None:
        """The Link to item dialog: (entity id, role), or ``None`` (tests replace this)."""
        assert self.session is not None
        dialog = LinkDialog(self.session, what, kinds, self)
        return dialog.chosen() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def _skip_files(self, files: list[UnlinkedFile]) -> None:
        """Leave these files out of scans: an exact exclude pattern on each one's root."""
        session = self.session
        assert session is not None

        def job() -> int:
            config = session.keep.config
            by_root: dict[str, list[str]] = {}
            for file in files:
                by_root.setdefault(file.root_id, []).append(exact_pattern(file.relpath))
            note = f"Skipped from Triage on {datetime.now():%Y-%m-%d}"
            for root_id, patterns in by_root.items():
                config = add_skipped(config, session.keep.dir, root_id, patterns, note)
            session.save_config(config)
            return len(files)

        def done(count: int) -> None:
            self.statusBar().showMessage(
                f"{count:,} file{'' if count == 1 else 's'} will be left out "
                "from the next scan (Keep configuration \u2192 Folders \u2192 Skip)."
            )
            if self.keep_config is not None:
                self.keep_config.reload()

        run_in_pool(job, on_done=done, on_error=lambda e: self.statusBar().showMessage(str(e)))

    def _save_hidden_columns(self, state_key: str, keys: list[str]) -> None:
        assert self.session is not None
        self._ui_state.setdefault("hidden_columns", {})[state_key] = keys
        save_ui_state(self.session.keep.ui_state_path, self._ui_state)

    def _save_view_state(self, kind: str, state_key: str, value: object) -> None:
        """Remember a view's layout or card lines (``None`` forgets the choice)."""
        assert self.session is not None
        views = self._ui_state.setdefault(kind, {})
        if value is None:
            views.pop(state_key, None)
        else:
            views[state_key] = value
        save_ui_state(self.session.keep.ui_state_path, self._ui_state)

    # --- the thumbnail cache ---

    def clear_thumbnail_cache(self) -> None:
        """Ask, then delete every stored thumbnail (DESIGN.md §10). Counting and clearing
        run in workers; thumbnails are made again as they are shown."""
        assert self.session is not None
        cache = self.session.thumbnails.cache
        self.clear_thumbnails_action.setEnabled(False)

        def counted(stats: CacheStats) -> None:
            if not stats.count:
                self.clear_thumbnails_action.setEnabled(True)
                self.statusBar().showMessage("The thumbnail cache is already empty.")
                return
            if not self.confirm_clear_thumbnails(stats):
                self.clear_thumbnails_action.setEnabled(True)
                return
            self.statusBar().showMessage("Clearing thumbnails…")
            run_in_pool(cache.clear, on_done=cleared, on_error=failed)

        def cleared(count: int) -> None:
            assert self.session is not None
            self.session.thumbnails.forget_failures()  # unreadable files get another try
            self.thumbnails.clear()
            for page in self.search_pages():
                page.grid.viewport().update()
            self.clear_thumbnails_action.setEnabled(True)
            self.statusBar().showMessage(f"Cleared {count:,} thumbnails.")
            if self.keep_config is not None:
                self.keep_config.show_cache_stats()

        def failed(error: BaseException) -> None:
            self.clear_thumbnails_action.setEnabled(True)
            logger.error("Clearing thumbnails failed", exc_info=error)
            self.statusBar().showMessage(f"Clearing thumbnails failed: {error}")

        run_in_pool(cache.stats, on_done=counted, on_error=failed)

    def confirm_clear_thumbnails(self, stats: CacheStats) -> bool:
        """Ask before clearing ``stats.count`` thumbnails (tests replace this)."""
        answer = QMessageBox.question(
            self,
            "Clear thumbnail cache",
            f"Delete {stats.count:,} stored thumbnails ({format_bytes(stats.bytes)})?\n\n"
            "They are made again as you browse, which reads the files again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    # --- thumbnail sizes ---

    def _make_size_menu(self) -> QMenu:
        menu = QMenu("Thumbnail size", self)
        self._fill_size_menu(menu)
        return menu

    def _fill_size_menu(self, menu: QMenu) -> None:
        """The size presets for the keep's largest size, then zooming (filled again when
        the largest size changes)."""
        assert self.session is not None
        menu.clear()
        self._size_group = QActionGroup(self)
        self._size_group.setExclusive(True)
        self.size_actions: dict[int, QAction] = {}
        for name, size in size_presets(self.session.thumbnail_max):
            action = QAction(f"{name} ({size} px)", self)
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, size=size: self.set_thumbnail_size(size)
            )
            self._size_group.addAction(action)
            menu.addAction(action)
            self.size_actions[size] = action
        menu.addSeparator()
        self.zoom_in_action = QAction("Zoom in", self)
        self.zoom_in_action.setShortcuts(
            [QKeySequence("Ctrl+="), QKeySequence(QKeySequence.StandardKey.ZoomIn)]
        )
        self.zoom_in_action.triggered.connect(lambda: self.zoom(1))
        self.zoom_out_action = QAction("Zoom out", self)
        self.zoom_out_action.setShortcuts([QKeySequence(QKeySequence.StandardKey.ZoomOut)])
        self.zoom_out_action.triggered.connect(lambda: self.zoom(-1))
        menu.addAction(self.zoom_in_action)
        menu.addAction(self.zoom_out_action)
        self._show_size()

    def thumbnail_max_changed(self) -> None:
        """The keep's largest thumbnail size changed: new presets, a size within them, and
        pictures made again at the new size."""
        self._fill_size_menu(self.size_menu)
        self.thumbnails.clear()
        self.set_thumbnail_size(self.thumbnail_size)
        for page in self.search_pages():
            page.grid.viewport().update()

    def set_thumbnail_size(self, size: int) -> None:
        """Show grid thumbnails at ``size`` pixels in every page, and remember it."""
        assert self.session is not None
        size = clamp_size(size, self.session.thumbnail_default, self.session.thumbnail_max)
        self.thumbnail_size = size
        for page in self.search_pages():
            page.set_thumbnail_size(size)
        self._show_size()
        self._ui_state["thumbnail_size"] = size
        save_ui_state(self.session.keep.ui_state_path, self._ui_state)

    def zoom(self, step: int) -> None:
        """One size bigger (``step`` > 0) or smaller."""
        assert self.session is not None
        self.set_thumbnail_size(zoomed(self.thumbnail_size, step, self.session.thumbnail_max))

    def _show_size(self) -> None:
        for size, action in self.size_actions.items():
            action.setChecked(size == self.thumbnail_size)
        if self.thumbnail_size not in self.size_actions:  # a zoom step between presets
            checked = self._size_group.checkedAction()
            if checked is not None:
                self._size_group.setExclusive(False)
                checked.setChecked(False)
                self._size_group.setExclusive(True)

    def _save_folded(self, folded: list[str]) -> None:
        assert self.session is not None
        self._ui_state["nav_folded"] = folded
        save_ui_state(self.session.keep.ui_state_path, self._ui_state)

    def _load_saved_searches(self) -> None:
        """Read the saved searches (names and definitions) in a worker, list them under
        SAVED, and close the page of one that's gone."""
        session = self.session
        assert session is not None

        def query() -> list[tuple[int, str, dict[str, Any]]]:
            with session.reader.connect() as conn:
                return list_saved(conn)

        def loaded(saved: list[tuple[int, str, dict[str, Any]]]) -> None:
            if not shiboken6.isValid(self.navigation):  # the window may have closed meanwhile
                return
            self._saved = {i: (n, d) for i, n, d in saved}
            self.navigation.set_saved([(i, n) for i, n, _ in saved])
            for target in [t for t in self._pages if t.kind == "saved"]:
                if int(target.key) not in self._saved:
                    self._close_page(target)

        run_in_pool(query, on_done=loaded)

    def _close_page(self, target: NavTarget) -> None:
        """Forget a page (its saved search was deleted); if it's showing, show Search all."""
        page = self._pages.pop(target, None)
        if page is None:
            return
        if self.stack.currentWidget() is page:
            self.navigation.select(NavTarget("search", label="Search all"))
        self.stack.removeWidget(page)
        page.deleteLater()

    def show_shortcuts(self) -> ShortcutsDialog:
        """Help → Keyboard shortcuts: a window listing them (not modal, so it can stay
        open beside the work)."""
        dialog = ShortcutsDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        return dialog

    # --- saved searches (#127) ---

    def save_search(self, *, as_new: bool, page: SearchPage | None = None) -> None:
        """Save a search page (the current one by default): a saved search's page in place
        (unless ``as_new``), else under a name asked for."""
        if page is None:
            shown = self.stack.currentWidget()
            page = shown if isinstance(shown, SearchPage) else None
        if page is None:
            self.statusBar().showMessage("Open a search to save it.")
            return
        definition = page.saved_definition()
        saved_id = self._saved_id_of(page)
        if saved_id is not None and not as_new and saved_id in self._saved:
            name = self._saved[saved_id][0]
            self.tag_actions.save_search(name, definition, saved_id)
            return
        default = self._saved[saved_id][0] if saved_id in self._saved else page.title
        chosen = self.choose_saved_name("Save search", default)
        if chosen is None:
            return
        name = chosen
        taken = next(
            (i for i, (n, _) in self._saved.items() if n.casefold() == name.strip().casefold()),
            None,
        )
        if taken is not None and not self.confirm_replace_saved(self._saved[taken][0]):
            return
        self.tag_actions.save_search(name, definition, taken)

    def _saved_id_of(self, page: SearchPage) -> int | None:
        target = next((t for t, p in self._pages.items() if p is page), None)
        return int(target.key) if target is not None and target.kind == "saved" else None

    def _saved_menu(self, target: NavTarget, point: QPoint) -> None:
        saved_id = int(target.key)
        menu = QMenu(self)
        menu.addAction("Open").triggered.connect(lambda: self.show_target(target))
        menu.addAction("Rename\u2026").triggered.connect(lambda: self.rename_saved(saved_id))
        delete = menu.addAction("Delete")
        delete.setToolTip("Edit \u2192 Undo brings it back")
        delete.triggered.connect(lambda: self.tag_actions.delete_saved(saved_id))
        menu.exec(point)

    def rename_saved(self, saved_id: int) -> None:
        if saved_id not in self._saved:
            return
        name = self.choose_saved_name("Rename saved search", self._saved[saved_id][0])
        if name is not None:
            self.tag_actions.rename_saved(saved_id, name)

    def choose_saved_name(self, title: str, default: str) -> str | None:
        """Ask for a saved search's name (tests replace this)."""
        name, ok = QInputDialog.getText(self, title, "Name:", text=default)
        return name if ok and name.strip() else None

    def confirm_replace_saved(self, name: str) -> bool:
        """Ask before saving over another search of that name (tests replace this)."""
        answer = QMessageBox.question(
            self,
            "Replace saved search",
            f"There's already a saved search named {name!r}. Replace it with this one?",
        )
        return answer == QMessageBox.StandardButton.Yes

    # --- scanning ---

    def scan_now(self) -> None:
        if self.session is not None:
            self.scans.scan(self.session)

    def configure_keep(self) -> KeepConfigWindow:
        """Show the Keep configuration window (one per main window)."""
        assert self.session is not None
        if self.keep_config is None:
            window = KeepConfigWindow(self.session, parent=self)
            window.changed.connect(self._config_changed)
            window.scan_requested.connect(self._scan_roots)
            window.thumbnail_max_changed.connect(self.thumbnail_max_changed)
            window.clear_thumbnails_requested.connect(self.clear_thumbnails_action.trigger)
            if self.lookups is not None:
                window.online_allowed.connect(self.lookups.everything)
            self.keep_config = window
        self.keep_config.show()
        self.keep_config.raise_()
        self.keep_config.activateWindow()
        return self.keep_config

    def _scan_roots(self, root_ids: list[str]) -> None:
        if self.session is not None and not self.scans.scan(self.session, root_ids):
            self.statusBar().showMessage("A scan is already running; try again when it's done.")

    # --- the activity panel ---

    def show_activity(self) -> None:
        self.activity.show()
        self.activity.raise_()

    def _activity_shown(self, shown: bool) -> None:
        if shown:
            self._show_folders()

    def _check_problems(self) -> None:
        """Show new problems (the log is written from workers) on the badge and panel."""
        session = self.session
        if session is None or session.problems.version == self._problems_seen:
            return
        self._problems_seen = session.problems.version
        problems = session.problems.items()
        self.problem_badge.setText(f"\u26a0 {len(problems):,}")
        self.problem_badge.setVisible(bool(problems))
        self.activity.show_problems(problems)

    def _clear_problems(self) -> None:
        if self.session is not None:
            self.session.problems.clear()
            self._check_problems()

    def _show_folders(self) -> None:
        """Read the roots' status in a worker for the activity panel."""
        session = self.session
        if session is None:
            return

        def job() -> dict[str, RootStatus]:
            with session.reader.connect() as conn:
                return root_statuses(conn)

        def done(statuses: dict[str, RootStatus]) -> None:
            if shiboken6.isValid(self):
                self.activity.show_folders(statuses)

        run_in_pool(job, on_done=done)

    def _config_changed(self, message: str) -> None:
        """Roots were renamed, moved, or removed: pages show the change."""
        self.statusBar().showMessage(message)
        self._show_folders()
        self._refresh_triage()
        self.thumbnails.clear()
        self._refresh_details()
        for page in self.search_pages():
            page.refresh()

    def _scan_started(self) -> None:
        self.scan_action.setEnabled(False)
        self.busy.setVisible(True)
        self.statusBar().showMessage("Scanning…")
        self.activity.set_now("Scanning…")

    def _scan_finished(self, reports: list[ScanReport]) -> None:
        self.scan_action.setEnabled(True)
        self.busy.setVisible(False)
        self.statusBar().showMessage(scan_summary(reports))
        self.activity.set_now(f"Last scan, {datetime.now():%H:%M}: {scan_summary(reports)}")
        self._show_folders()
        self._check_problems()
        self.thumbnails.clear()  # files may have changed
        if self.keep_config is not None:
            self.keep_config.reload()
        self._refresh_triage()
        self.refresh_keywords(after_scan=True)
        self._queue_thumbnails()
        self.read_contents()
        if self.lookups is not None and self.session is not None:
            self.lookups.after_scan(self.session.last_scan_started)
        for detail in self._pages.values():
            if isinstance(detail, DetailPage):
                detail.refresh()
        for page in self.search_pages():
            page.refresh()

    def _queue_thumbnails(self) -> None:
        """After a scan: make its thumbnails in the background (§6 step 7)."""
        session = self.session
        if session is None:
            return

        def queued(count: int) -> None:
            if count and shiboken6.isValid(self):
                self._show_queue(0, count)

        run_in_pool(
            lambda: session.queue_thumbnails(
                progress=self._queue_progress.emit, done=self._queue_done.emit
            ),
            on_done=queued,
        )

    def _show_queue(self, done: int, total: int) -> None:
        left = total - done
        self.thumbnail_status.setText(f"Making thumbnails: {left:,} left")
        self.thumbnail_status.setToolTip(
            f"New and changed items' thumbnails, {done:,} of {total:,} made. Pages you "
            "look at go first."
        )
        self.thumbnail_status.setVisible(left > 0)
        self.activity.set_queue(left)

    def _queue_finished(self, result: QueueResult) -> None:
        self.thumbnail_status.setVisible(False)
        self.activity.set_queue(0)
        self.thumbnails.clear()  # grids pick up what was made from the cache
        for page in self.search_pages():
            page.grid.viewport().update()
        logger.info("Made %d background thumbnails (%d pictures)", result.done, result.pictures)

    # --- reading documents' text (#348; core.contents) ---

    def read_contents(self) -> None:
        """Read the text of new and changed documents in the background (when the keep
        searches inside documents)."""
        session = self.session
        if session is None or session.keep.config.contents_index is None:
            return

        def queued(count: int) -> None:
            if count and shiboken6.isValid(self):
                self._show_contents(0, count)

        run_in_pool(
            lambda: session.queue_contents(
                progress=self._contents_progress.emit, done=self._contents_done.emit
            ),
            on_done=queued,
            on_error=lambda e: logger.error("Reading documents' text failed", exc_info=e),
        )

    def _show_contents(self, done: int, total: int) -> None:
        left = total - done
        self.contents_status.setText(f"Reading contents: {left:,} left")
        self.contents_status.setToolTip(
            f"The text of new and changed documents, for searching inside them: {done:,} of "
            f"{total:,} read"
        )
        self.contents_status.setVisible(left > 0)

    def _contents_finished(self, result: ContentsResult) -> None:
        self.contents_status.setVisible(False)
        self._check_problems()
        logger.info("Read %d documents' text (%d pages)", result.read, result.pages)

    # --- online lookups (#340; ui.lookups) ---

    def look_up_online(self, entity_ids: list[int]) -> None:
        """Look up online: these items' details, fetched again (asking first, once)."""
        if self.lookups is not None:
            self.lookups.look_up(entity_ids)

    def _show_lookups(self, done: int, total: int) -> None:
        left = total - done
        self.lookup_status.setText(f"Looking up online: {left:,} left")
        self.lookup_status.setToolTip(f"Items' details from online services, {done:,} of {total:,}")
        self.lookup_status.setVisible(left > 0)

    def _lookups_finished(self, report: LookupReport, by_user: bool) -> None:
        self.lookup_status.setVisible(False)
        summary = lookup_summary(report, by_user)
        if summary:
            self.statusBar().showMessage(summary)
            self.activity.set_now(f"Online, {datetime.now():%H:%M}: {summary}")
        self._check_problems()
        if report.items:
            self._refresh_details()
            for page in self.search_pages():
                page.refresh()

    def _online_consent_changed(self) -> None:
        if self.keep_config is not None:
            self.keep_config.reload()

    def _scan_failed(self, error: BaseException) -> None:
        self.scan_action.setEnabled(True)
        self.busy.setVisible(False)
        self.statusBar().showMessage(f"Scan failed: {error}")
        self.activity.set_now(f"The last scan failed: {error}")
        if self.session is not None:
            self.session.problems.add([Problem(SCAN, str(error))])
            self._check_problems()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.session is not None and self.scans.running:
            self.statusBar().showMessage("Wait for the scan to finish before closing.")
            event.ignore()
            return
        super().closeEvent(event)
        if event.isAccepted():
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            if self.session is not None:
                # Thumbnail jobs read the keep: let them finish before it closes.
                self.thumbnails.clear()
                self.thumbnails.wait()
            self.closed.emit()


def scan_summary(reports: list[ScanReport]) -> str:
    """One status-bar line for a finished scan."""
    new = sum(r.new for r in reports)
    changed = sum(r.changed for r in reports)
    missing = sum(r.missing for r in reports)
    moved = sum(len(r.moves) for r in reports)
    parts = [f"{new} new", f"{changed} changed", f"{missing} missing"]
    if moved:
        parts.append(f"{moved} moved")
    skipped = sum(r.skipped for r in reports)
    if skipped:
        parts.append(f"{skipped} skipped")
    text = "Scan finished: " + ", ".join(parts) + "."
    offline = [r.root_id for r in reports if not r.online]
    if offline:
        text += f" Offline: {', '.join(offline)}."
    failed = sum(len(r.ingest_errors) for r in reports)
    if failed:
        text += f" {failed} file{'s' if failed != 1 else ''} couldn't be read."
    return text


def _ext(relpath: str) -> str:
    """A path's extension, lowercased with its dot (``".png"``), or ``""``."""
    name = relpath.rpartition("/")[2]
    stem, dot, ext = name.rpartition(".")
    return f".{ext.lower()}" if dot and stem else ""
