"""Shared set-up for the GUI tests."""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from PySide6.QtCore import QEvent, QObject, Qt, QThreadPool, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QFileDialog,
    QFontDialog,
    QInputDialog,
    QMenu,
    QMessageBox,
    QWidget,
)

from tagalot.core.handlers import Command, FileToOpen
from tagalot.ui import main_window
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.navigation import NavTarget

LAUNCHED: list[tuple[str, str]] = []
"""What GUI tests asked the OS to launch, instead of launching it: (how, path or program)."""


@pytest.fixture(autouse=True)
def nothing_is_launched(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str]]]:
    """Opening a file in a test must never start a real program on the machine."""

    def record(how: str) -> object:
        def launch(self: FileOpener, target: FileToOpen | Command) -> None:
            LAUNCHED.append(
                (how, target.path if isinstance(target, FileToOpen) else target.program)
            )

        return launch

    for how in ("open", "reveal", "open_with", "start"):
        monkeypatch.setattr(FileOpener, how, record(how))
    LAUNCHED.clear()
    yield LAUNCHED
    LAUNCHED.clear()


class UnexpectedModal(AssertionError):
    """A test opened a dialog, message box, or menu that nothing in the test answers."""


MODALS: list[tuple[type, str]] = [
    (QDialog, "exec"),  # every dialog class Tagalot defines, unless a test patches its own
    *((QMessageBox, n) for n in ("warning", "critical", "information", "question", "about")),
    *((QInputDialog, n) for n in ("getText", "getItem", "getInt", "getDouble", "getMultiLineText")),
    *(
        (QFileDialog, n)
        for n in ("getExistingDirectory", "getOpenFileName", "getOpenFileNames", "getSaveFileName")
    ),
    (QColorDialog, "getColor"),
    (QFontDialog, "getFont"),
]


def _describe(args: tuple[Any, ...]) -> str:
    """Whatever identifies the modal: a dialog's or menu's title, or a message box's title
    and text."""
    words = [a for a in args if isinstance(a, str)]
    widget = next((a for a in args if isinstance(a, QWidget)), None)
    if widget is not None and isinstance(widget, QDialog | QMenu):
        title = widget.windowTitle() if isinstance(widget, QDialog) else widget.title()
        names = [a.text() for a in widget.actions()] if isinstance(widget, QMenu) else []
        words = [f"{type(widget).__name__} {title!r}", *names, *words]
    return "; ".join(words) or "no title"


class _MenuCatcher(QObject):
    """Closes every menu as it appears, and remembers it.

    ``QMenu.exec`` can't be patched like the others: PySide resolves a menu's ``exec`` to
    the built-in method whatever the class attribute says (it has a static form too). So
    menus are caught as they are shown instead; closing one ends its ``exec``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.caught: list[str] = []

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(watched, QMenu):
            self.caught.append(_describe((watched,)))
            QTimer.singleShot(0, watched.close)
        return False


@pytest.fixture(autouse=True)
def no_unexpected_modals(
    monkeypatch: pytest.MonkeyPatch, qapp: QApplication
) -> Iterator["_MenuCatcher"]:
    """Fail, naming it, when a test opens a modal it doesn't answer (#272).

    Offscreen, nothing ever closes a modal dialog, message box, or context menu, so it
    would wait forever and hang the run. Dialogs and message boxes raise
    :class:`UnexpectedModal` at once; a menu is closed as it appears and the test fails
    when it ends. A test that means to open a dialog patches it itself
    (``monkeypatch.setattr(QMessageBox, "question", ...)``, or the dialog class's ``exec``),
    which takes precedence over this. A test of a menu calls its actions directly (as the
    menu builders, such as ``SearchPage.item_menu``, allow) rather than showing it.
    """

    def refuse(cls: type, name: str) -> Callable[..., Any]:
        def unexpected(*args: Any, **kwargs: Any) -> Any:
            raise UnexpectedModal(
                f"{cls.__name__}.{name} ({_describe(args)}) opened in a test that doesn't "
                "answer it; it would wait forever. Patch it in the test."
            )

        return unexpected

    for cls, name in MODALS:
        monkeypatch.setattr(cls, name, refuse(cls, name))
    catcher = _MenuCatcher()
    qapp.installEventFilter(catcher)
    try:
        yield catcher
    finally:
        qapp.removeEventFilter(catcher)
    if catcher.caught:
        raise UnexpectedModal(
            f"A menu opened in a test that doesn't answer it ({'; '.join(catcher.caught)}); "
            "its exec() would wait forever. Call the menu's actions directly instead."
        )


POOL_DRAIN_MS = 10_000
"""How long a test's leftover background jobs may take to finish after it."""


@pytest.fixture(autouse=True)
def background_jobs_finish() -> Iterator[None]:
    """Each GUI test's background jobs finish before the next test starts (#374).

    The window runs searches, index builds, and saves on Qt's shared thread pool
    (``ui.workers.run_in_pool``). A job a test leaves running (a search its last change
    started) would otherwise overlap the next test, sharing its threads. They take
    milliseconds; one still running after :data:`POOL_DRAIN_MS` is stuck, and fails the
    test that left it.
    """
    yield
    pool = QThreadPool.globalInstance()
    if not pool.waitForDone(POOL_DRAIN_MS):
        pytest.fail(
            f"{pool.activeThreadCount()} background job(s) still running "
            f"{POOL_DRAIN_MS / 1000:.0f} s after the test ended"
        )


@pytest.fixture(autouse=True)
def windows_open_on_search_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most GUI tests start from Search all, as windows did before the dashboard became the
    opening page (#115); ``test_opening.py`` checks the real opening page."""
    monkeypatch.setattr(main_window, "OPENING_PAGE", NavTarget("search", label="Search all"))


@pytest.fixture(autouse=True)
def no_modifiers_left_held() -> Iterator[None]:
    """Forget any keyboard modifier a test's key presses left "held".

    ``QTest.keyClick(widget, key, Shift)`` leaves Qt believing Shift is still down, and
    that changes how later tests' selections behave (``selectRow`` then extends or toggles).
    A plain key release afterwards resets Qt's idea of the modifiers.
    """
    yield
    if QApplication.instance() is None:
        return
    widget = QWidget()
    QTest.keyRelease(widget, Qt.Key.Key_Shift, Qt.KeyboardModifier.NoModifier)
    widget.deleteLater()
