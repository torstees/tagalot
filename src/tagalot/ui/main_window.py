"""Main window: navigation, center stack, tagging panel, status bar (DESIGN.md §12).

Layout: a slim toolbar ("Scan now", the keep's name); the navigation pane on the left and
the current view in the center; a dockable tagging panel on the right; a status bar showing
scan progress. Views that arrive in later milestones show a labelled placeholder.
"""

import logging
from collections.abc import Callable

import shiboken6
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QLabel,
    QMainWindow,
    QProgressBar,
    QSplitter,
    QStackedWidget,
    QStyle,
    QToolBar,
    QWidget,
)
from sqlalchemy import select

from tagalot.core.models import SavedSearch
from tagalot.core.scanjob import ScanReport
from tagalot.core.search_fields import view_spec
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.tags import PATH_SEPARATOR, TagError, split_tag_path, tag_counts
from tagalot.core.ui_state import load_ui_state, save_ui_state
from tagalot.themes.api import SearchView
from tagalot.ui.navigation import NavigationPane, NavTarget
from tagalot.ui.search_view import SearchPage
from tagalot.ui.tag_actions import TagActions
from tagalot.ui.tag_panel import TagPanel
from tagalot.ui.workers import ScanController, run_in_pool

logger = logging.getLogger(__name__)

WINDOW_TITLE = "Tagalot"

SUMMARY_DELAY_MS = 150
"""How long the Tags panel's selection summary waits for the selection to settle."""

_COMING = {
    "dashboard": "The dashboard arrives in M15.",
    "saved": "Saved searches arrive in M18.",
    "triage": "Triage arrives in M14.",
    "dedupe": "Dedupe arrives in M16.",
    "tags": "The tag manager arrives in M7.",
}


class MainWindow(QMainWindow):
    """The top-level window, for an open keep or (with no session) an empty placeholder.

    Emits :attr:`closed` once the window has actually closed (not when a close was refused
    because a scan is running), so its owner can close the keep.
    """

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
        keep_menu.addSeparator()
        if on_open_other is not None:
            self.open_other_action = QAction("Open another keep…", self)
            self.open_other_action.setShortcut(QKeySequence.StandardKey.Open)
            self.open_other_action.triggered.connect(on_open_other)
            keep_menu.addAction(self.open_other_action)
        keep_menu.addAction(close_action)

        # Edit: undo and redo tagging and tag operations (DESIGN.md §7).
        self.tag_actions = TagActions(session, self)
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

        # Toolbar.
        toolbar = QToolBar("Main toolbar", self)
        toolbar.setObjectName("main_toolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.addAction(self.scan_action)
        keep_label = QLabel(f"  {session.keep.config.name}")
        keep_label.setToolTip(str(session.keep.dir))
        toolbar.addWidget(keep_label)
        self.addToolBar(toolbar)

        # Navigation and the center stack.
        views = [v.name for v in session.theme.views if isinstance(v, SearchView)]
        self.navigation = NavigationPane(views)
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

        # Status bar: a message plus a busy indicator while scanning.
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

    def show_target(self, target: NavTarget) -> None:
        """Show the page for ``target``, creating it on first use."""
        page = self._pages.get(target)
        if page is None:
            page = self._make_page(target)
            self._pages[target] = page
            self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)
        self._schedule_summary()

    def _make_page(self, target: NavTarget) -> QWidget:
        session = self.session
        assert session is not None
        if target.kind in ("search", "view"):
            # Which columns are hidden is remembered per view, in ui_state.json.
            state_key = f"{target.kind}:{target.key}"
            hidden = self._ui_state.get("hidden_columns", {}).get(state_key)
            if target.kind == "search":
                spec = SearchSpec()
            else:
                views = [v for v in session.theme.views if isinstance(v, SearchView)]
                view = next(v for v in views if v.name == target.key)
                spec = view_spec(session.schema, view)
            search = SearchPage(
                session,
                target.label,
                spec,
                grouped=target.kind == "search",
                hidden_columns=hidden,
            )
            search.tags_dropped.connect(self.apply_tags)
            search.selection_changed.connect(self._schedule_summary)
            search.hidden_columns_changed.connect(
                lambda keys: self._save_hidden_columns(state_key, keys)
            )
            return search
        page = QLabel(f"{target.label}\n\n{_COMING[target.kind]}")
        page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return page

    # --- tagging ---

    def apply_tags(self, entity_ids: list[int], tag_ids: list[int]) -> None:
        """Tag entities (a drop on results), in the background; undoable."""
        self.tag_actions.apply(entity_ids, tag_ids, self.tag_names(tag_ids))

    def tag_selection(self, tag_ids: list[int], remove: bool = False) -> None:
        """Apply (or remove) tags on the current page's selected items (Enter or
        Shift+Enter in the Tags panel)."""
        page = self.stack.currentWidget()
        if not isinstance(page, SearchPage):
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
        page = self.stack.currentWidget()
        if isinstance(page, SearchPage):
            page.selected_entity_ids(lambda ids: self.tag_actions.create(names, ids, path))
        else:
            self.tag_actions.create(names, [], path)

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
        page = self.stack.currentWidget()
        self._summary_generation += 1
        generation = self._summary_generation
        if session is None or not isinstance(page, SearchPage):
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
        self._schedule_summary()
        for page in self.search_pages():
            page.refresh()  # tag filters may now match differently
        self.tag_panel.reload()  # undo can change the tag tree
        self._update_undo_actions()

    def _tag_message(self, message: str) -> None:
        self.statusBar().showMessage(message)
        self._update_undo_actions()

    def _update_undo_actions(self) -> None:
        for action, verb, label in (
            (self.undo_action, "Undo", self.tag_actions.undo_label),
            (self.redo_action, "Redo", self.tag_actions.redo_label),
        ):
            action.setEnabled(label is not None)
            action.setText(f"{verb} {label}" if label else verb)

    def search_pages(self) -> list[SearchPage]:
        """The search pages created so far."""
        return [p for p in self._pages.values() if isinstance(p, SearchPage)]

    def _save_hidden_columns(self, state_key: str, keys: list[str]) -> None:
        assert self.session is not None
        self._ui_state.setdefault("hidden_columns", {})[state_key] = keys
        save_ui_state(self.session.keep.ui_state_path, self._ui_state)

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

    def _scan_started(self) -> None:
        self.scan_action.setEnabled(False)
        self.busy.setVisible(True)
        self.statusBar().showMessage("Scanning…")

    def _scan_finished(self, reports: list[ScanReport]) -> None:
        self.scan_action.setEnabled(True)
        self.busy.setVisible(False)
        self.statusBar().showMessage(scan_summary(reports))
        for page in self.search_pages():
            page.refresh()

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
