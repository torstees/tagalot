"""Main window: navigation, center stack, tagging panel, status bar (DESIGN.md §12).

Layout: a slim toolbar ("Scan now", the keep's name); the navigation pane on the left and
the current view in the center; a dockable tagging panel on the right; a status bar showing
scan progress. Views that arrive in later milestones show a labelled placeholder.
"""

import logging
from collections.abc import Callable

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeySequence, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QStackedWidget,
    QStyle,
    QToolBar,
    QWidget,
)
from sqlalchemy import select

from tagalot.core.actions import ActionResult
from tagalot.core.formats import format_bytes
from tagalot.core.handlers import OPEN, REVEAL, FileToOpen
from tagalot.core.models import SavedSearch
from tagalot.core.root_admin import edit_root
from tagalot.core.scanjob import ScanReport
from tagalot.core.search_fields import view_spec
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.tags import (
    PATH_SEPARATOR,
    DeleteMode,
    TagError,
    split_tag_path,
    tag_counts,
)
from tagalot.core.thumbnails.cache import CacheStats
from tagalot.core.thumbnails.queue import QueueResult
from tagalot.core.triage import UnlinkedFile, exact_pattern
from tagalot.core.ui_state import load_ui_state, save_ui_state
from tagalot.themes.api import SearchView
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.keep_config import KeepConfigWindow
from tagalot.ui.navigation import NavigationPane, NavTarget
from tagalot.ui.search_view import SearchPage
from tagalot.ui.tag_actions import TagActions
from tagalot.ui.tag_manager import TagManagerPage
from tagalot.ui.tag_panel import TagPanel
from tagalot.ui.thumbnails import ThumbnailLoader, clamp_size, size_presets, zoomed
from tagalot.ui.triage import TriagePage
from tagalot.ui.workers import ScanController, run_in_pool

logger = logging.getLogger(__name__)

WINDOW_TITLE = "Tagalot"

SUMMARY_DELAY_MS = 150
"""How long the Tags panel's selection summary waits for the selection to settle."""

NAVIGATION_MIN_WIDTH = 170
"""The navigation pane never gets narrower than this, however wide a page wants to be."""

_COMING = {
    "dashboard": "The dashboard arrives in M15.",
    "saved": "Saved searches arrive in M18.",
    "dedupe": "Dedupe arrives in M16.",
}


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
        view_menu.addSeparator()
        self.size_menu = self._make_size_menu()
        view_menu.addMenu(self.size_menu)

        # Status bar: a message plus a busy indicator while scanning.
        self.thumbnail_status = QLabel()
        self.thumbnail_status.setObjectName("thumbnail_status")
        self.thumbnail_status.setVisible(False)
        self.statusBar().addPermanentWidget(self.thumbnail_status)
        self._queue_progress.connect(self._show_queue)
        self._queue_done.connect(self._queue_finished)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setMaximumWidth(120)
        self.busy.setVisible(False)
        self.statusBar().addPermanentWidget(self.busy)
        self.statusBar().showMessage("Ready")
        self.scans.started.connect(self._scan_started)
        self.scans.progress.connect(self.statusBar().showMessage)
        self.scans.finished.connect(self._scan_finished)
        self.scans.failed.connect(self._scan_failed)

        self._load_saved_searches()
        self.navigation.select(NavTarget("search", label="Search all"))

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
            detail.action_requested.connect(self.run_action)
            detail.file_opener = self.files
            detail.show_in_search.connect(lambda _id: self._contents_in_search(detail))
            detail.selection_changed.connect(self._schedule_summary)
            return detail
        if target.kind == "triage":
            triage = TriagePage(
                session,
                lambda title, spec, key: self._search_page(title, spec, key),
                self.files,
            )
            triage.dismiss_requested.connect(self.tag_actions.dismiss)
            triage.delete_requested.connect(self.delete_items)
            triage.skip_requested.connect(self._skip_files)
            triage.message.connect(lambda text: self.statusBar().showMessage(text, 8000))
            for listed in (triage.untagged, triage.missing):
                listed.selection_changed.connect(self._schedule_summary)
            triage.tabs.currentChanged.connect(lambda _: self._schedule_summary())
            return triage
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
        layout = self._ui_state.get("layouts", {}).get(state_key, layout)
        card_lines = self._ui_state.get("card_lines", {}).get(state_key)
        toggles = self._ui_state.get("toggles", {}).get(state_key)
        search = SearchPage(
            session,
            title,
            spec,
            grouped=grouped,
            hidden_columns=hidden,
            layout_mode=layout,
            card_lines=card_lines if isinstance(card_lines, list) else None,
            thumbnails=self.thumbnails,
            thumbnail_size=self.thumbnail_size,
            preview=self.preview_action.isChecked(),
            toggles=toggles if isinstance(toggles, dict) else None,
        )
        search.size_menu = self.size_menu
        search.open_requested.connect(self.open_entity)
        search.reread_requested.connect(self.reread)
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
        return search

    def _preview_toggled(self, visible: bool) -> None:
        """Show or hide the preview strip on every search page, and remember it."""
        assert self.session is not None
        for page in self.search_pages():
            page.set_preview_visible(visible)
        self._ui_state["preview"] = visible
        save_ui_state(self.session.keep.ui_state_path, self._ui_state)

    def _contents_search(self, entity_type: str, spec: SearchSpec) -> SearchPage:
        """The search embedded in a container's detail page. Pages of the same type share
        their remembered layout and columns."""
        return self._search_page("Contents", spec, f"contents:{entity_type}", layout="grid")

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

    def run_action(self, method: str, entity_ids: list[int]) -> None:
        """Run a theme action (a menu entry or a page's button), then open what it asks."""
        assert self.session is not None
        spec = self.session.theme.actions().get(method)
        self.statusBar().showMessage(f"{spec.label if spec else method}\u2026")
        self.tag_actions.run_action(method, entity_ids, self._action_outputs)

    def _action_outputs(self, result: ActionResult) -> None:
        for kind, value in result.outputs:
            if kind in ("open", "reveal"):
                self.files.act(FileToOpen(0, value, False), OPEN if kind == "open" else REVEAL)

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

        def show(count: int, counts: dict[int, int]) -> None:
            if generation == self._summary_generation and shiboken6.isValid(self.tag_panel):
                self.tag_panel.set_selection(count, counts)

        def selected(ids: list[int]) -> None:
            if not ids:
                show(0, {})
                return

            def count() -> dict[int, int]:
                with session.reader.connect() as conn:
                    return tag_counts(conn, ids)

            run_in_pool(count, on_done=lambda counts: show(len(ids), counts))

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
            elif isinstance(page, DetailPage) and page.contents is not None:
                pages.append(page.contents)
            elif isinstance(page, TriagePage):
                pages += [page.untagged, page.missing]
        return pages

    def _refresh_triage(self) -> None:
        for page in self._pages.values():
            if isinstance(page, TriagePage):
                page.refresh()

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

    def _skip_files(self, files: list[UnlinkedFile]) -> None:
        """Leave these files out of scans: an exact exclude pattern on each one's root."""
        session = self.session
        assert session is not None

        def job() -> int:
            config = session.keep.config
            by_root: dict[str, list[str]] = {}
            for file in files:
                by_root.setdefault(file.root_id, []).append(exact_pattern(file.relpath))
            for root_id, patterns in by_root.items():
                root = next(r for r in config.roots if r.id == root_id)
                new = [p for p in patterns if p not in root.exclude]
                config = edit_root(config, session.keep.dir, root_id, exclude=root.exclude + new)
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
        session = self.session
        assert session is not None

        def query() -> list[tuple[int, str]]:
            with session.reader.connect() as conn:
                rows = conn.execute(
                    select(SavedSearch.id, SavedSearch.name).order_by(SavedSearch.name)
                )
                return [(i, n) for i, n in rows]

        def loaded(saved: list[tuple[int, str]]) -> None:
            if shiboken6.isValid(self.navigation):  # the window may have closed meanwhile
                self.navigation.set_saved(saved)

        run_in_pool(query, on_done=loaded)

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
            self.keep_config = window
        self.keep_config.show()
        self.keep_config.raise_()
        self.keep_config.activateWindow()
        return self.keep_config

    def _scan_roots(self, root_ids: list[str]) -> None:
        if self.session is not None and not self.scans.scan(self.session, root_ids):
            self.statusBar().showMessage("A scan is already running; try again when it's done.")

    def _config_changed(self, message: str) -> None:
        """Roots were renamed, moved, or removed: pages show the change."""
        self.statusBar().showMessage(message)
        self.thumbnails.clear()
        self._refresh_details()
        for page in self.search_pages():
            page.refresh()

    def _scan_started(self) -> None:
        self.scan_action.setEnabled(False)
        self.busy.setVisible(True)
        self.statusBar().showMessage("Scanning…")

    def _scan_finished(self, reports: list[ScanReport]) -> None:
        self.scan_action.setEnabled(True)
        self.busy.setVisible(False)
        self.statusBar().showMessage(scan_summary(reports))
        self.thumbnails.clear()  # files may have changed
        if self.keep_config is not None:
            self.keep_config.reload()
        self._refresh_triage()
        self._queue_thumbnails()
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

    def _queue_finished(self, result: QueueResult) -> None:
        self.thumbnail_status.setVisible(False)
        self.thumbnails.clear()  # grids pick up what was made from the cache
        for page in self.search_pages():
            page.grid.viewport().update()
        logger.info("Made %d background thumbnails (%d pictures)", result.done, result.pictures)

    def _scan_failed(self, error: BaseException) -> None:
        self.scan_action.setEnabled(True)
        self.busy.setVisible(False)
        self.statusBar().showMessage(f"Scan failed: {error}")

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
    text = "Scan finished: " + ", ".join(parts) + "."
    offline = [r.root_id for r in reports if not r.online]
    if offline:
        text += f" Offline: {', '.join(offline)}."
    failed = sum(len(r.ingest_errors) for r in reports)
    if failed:
        text += f" {failed} file{'s' if failed != 1 else ''} couldn't be read."
    return text
