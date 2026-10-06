"""Looking items up online from the window (DESIGN.md §9 *Online details*, #340): after
each scan, for what it made or changed, and on demand with **Look up online**; asking once
per keep first, and only when something would be sent.

One run at a time, in a worker; requests made meanwhile wait their turn. The work itself
is ``KeepSession.look_up`` (``core.online``).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import shiboken6
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from tagalot.core.online import LookupReport, consent_text
from tagalot.core.session import KeepSession
from tagalot.ui.online import ALLOW, NEVER, ask_online_consent
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

LOOK_UP_TIP = (
    "Look these items' details up online again, by their identifiers (a DOI, an ISBN). "
    "Your edits are never replaced."
)


@dataclass(frozen=True)
class Request:
    ids: list[int] | None
    """The items, or ``None`` for every item (once lookups are allowed)."""
    refresh: bool
    by_user: bool


class OnlineLookups(QObject):
    """The window's online lookups; see the module docstring."""

    progressed = Signal(int, int)
    """``(items done, items in all)`` of the run in progress (from its worker)."""
    finished = Signal(object, bool)
    """A run ended: its :class:`LookupReport`, and whether the user asked for it."""
    message = Signal(str)
    """Something to say in the status bar."""
    consent_changed = Signal()
    """``[online] lookups`` was saved."""

    def __init__(self, session: KeepSession, parent: QWidget) -> None:
        super().__init__(parent)
        self.session = session
        self.window = parent
        self.running = False
        self._waiting: list[Request] = []
        self._asking = False
        self.not_now = False
        """The user said Not now: not asked again until the keep is opened again, except
        by Look up online."""

    @property
    def available(self) -> bool:
        """The keep's theme looks anything up."""
        return bool(self.session.theme.online_sources)

    # --- what starts a lookup ---

    def look_up(self, entity_ids: list[int]) -> None:
        """**Look up online**: these items, fetching again what was fetched before."""
        if self.available and entity_ids:
            self._consent_then(Request(list(entity_ids), refresh=True, by_user=True))

    def after_scan(self, since: datetime | None) -> None:
        """After a scan that began at ``since``: what it made or changed."""
        if not self.available or self.session.keep.config.online_lookups == NEVER:
            return
        if self.session.keep.config.online_lookups is None and self.not_now:
            return
        session = self.session
        run_in_pool(
            lambda: session.lookup_candidates(since),
            on_done=lambda ids: self._consent_then(Request(ids, False, False)) if ids else None,
            on_error=lambda e: logger.error("Finding items to look up failed", exc_info=e),
        )

    def everything(self) -> None:
        """Once lookups are allowed: every item without kept answers."""
        if self.available and self.session.keep.config.online_lookups == ALLOW:
            self._start(Request(None, refresh=False, by_user=False))

    # --- consent ---

    def _consent_then(self, request: Request) -> None:
        answer = self.session.keep.config.online_lookups
        if answer == ALLOW:
            self._start(request)
        elif answer == NEVER:
            if request.by_user and self.turn_on():
                self._save(ALLOW, then=lambda: self._start(request))
        else:
            self._ask_if_needed(request)

    def _ask_if_needed(self, request: Request) -> None:
        """Not asked yet: ask, but only if this would send something."""
        session = self.session
        ids = request.ids or []

        def counted(count: int) -> None:
            if not shiboken6.isValid(self) or self._asking:
                return
            if count == 0:
                if request.by_user:
                    self.message.emit("Nothing to look up online for these items.")
                return
            if session.keep.config.online_lookups is not None:  # answered meanwhile
                self._consent_then(request)
                return
            self._asking = True
            try:
                answer = self.ask()
            finally:
                self._asking = False
            if answer == ALLOW:

                def allowed() -> None:
                    self._start(request)
                    self.everything()  # and what's already in the keep

                self._save(ALLOW, then=allowed)
            elif answer == NEVER:
                self._save(NEVER)
            else:
                self.not_now = True
                self.message.emit("Nothing was looked up online.")

        run_in_pool(
            lambda: session.to_fetch(ids, refresh=request.refresh),
            on_done=counted,
            on_error=lambda e: logger.error("Planning a lookup failed", exc_info=e),
        )

    def ask(self) -> str | None:
        """The once-per-keep question (tests replace it)."""
        return ask_online_consent(self.window, list(self.session.theme.online_sources))

    def turn_on(self) -> bool:
        """Lookups are off (Never) and the user asked for one: offer to turn them on."""
        answer = QMessageBox.question(
            self.window,
            "Look up online",
            "Online lookups are off for this keep. Turn them on?\n\n"
            f"{consent_text(self.session.theme.online_sources)}",
        )
        return answer == QMessageBox.StandardButton.Yes

    def _save(self, answer: str, then: Callable[[], None] | None = None) -> None:
        session = self.session

        def saved(_: object) -> None:
            if not shiboken6.isValid(self):
                return
            self.consent_changed.emit()
            if then is not None:
                then()

        run_in_pool(
            lambda: session.set_online_lookups(answer),
            on_done=saved,
            on_error=lambda e: self.message.emit(f"Couldn't save the keep's settings: {e}"),
        )

    # --- running ---

    def _start(self, request: Request) -> None:
        if self.running:
            self._waiting.append(request)
            return
        self.running = True
        session = self.session
        if request.by_user:
            self.message.emit("Looking up online…")

        def work() -> LookupReport:
            ids = request.ids if request.ids is not None else session.lookup_candidates(None)
            return session.look_up(ids, refresh=request.refresh, progress=self.progressed.emit)

        def failed(error: BaseException) -> None:
            if not shiboken6.isValid(self):
                return
            logger.error("Looking up online failed", exc_info=error)
            self.message.emit(f"Looking up online failed: {error}")
            self._next()

        def done(report: LookupReport) -> None:
            if not shiboken6.isValid(self):
                return
            self.finished.emit(report, request.by_user)
            self._next()

        run_in_pool(work, on_done=done, on_error=failed)

    def _next(self) -> None:
        self.running = False
        if self._waiting:
            self._consent_then(self._waiting.pop(0))


def lookup_summary(report: LookupReport, by_user: bool) -> str:
    """One status-bar line for a finished lookup; empty for a quiet automatic one."""
    parts = []
    if report.items or by_user:
        noun = "item" if report.items == 1 else "items"
        parts.append(f"Looked up {report.items:,} {noun} online")
    if report.not_found:
        parts.append(f"{report.not_found:,} not found")
    if report.skipped:
        parts.append(f"{report.skipped:,} left for later (a service couldn't be reached)")
    if not parts:
        return ""
    return ", ".join(parts) + "."
