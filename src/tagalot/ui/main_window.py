"""Main window: navigation, center stack, tagging panel, status bar (DESIGN.md §12).

Layout: a slim toolbar ("Scan now", the keep's name); the navigation pane on the left and
the current view in the center; a dockable tagging panel on the right; a status bar showing
scan progress. Views that arrive in later milestones show a labelled placeholder.
"""

import logging
from collections.abc import Callable

import shiboken6
from PySide6.QtCore import Qt, Signal
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
from tagalot.core.ui_state import load_ui_state, save_ui_state
from tagalot.themes.api import SearchView
from tagalot.ui.navigation import NavigationPane, NavTarget
from tagalot.ui.search_view import SearchPage
from tagalot.ui.tag_panel import TagPanel
from tagalot.ui.workers import ScanController, run_in_pool

logger = logging.getLogger(__name__)

WINDOW_TITLE = "Tagalot"

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

    def _make_page(self, target: NavTarget) -> QWidget:
        session = self.session
        assert session is not None
        if target.kind == "search":
            return SearchPage(session, target.label, SearchSpec(), grouped=True)
        if target.kind == "view":
            views = [v for v in session.theme.views if isinstance(v, SearchView)]
            view = next(v for v in views if v.name == target.key)
            return SearchPage(session, target.label, view_spec(session.schema, view))
        page = QLabel(f"{target.label}\n\n{_COMING[target.kind]}")
        page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return page

    def search_pages(self) -> list[SearchPage]:
        """The search pages created so far."""
        return [p for p in self._pages.values() if isinstance(p, SearchPage)]

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
