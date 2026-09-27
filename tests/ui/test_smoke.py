"""End-to-end smoke tests: the app as a user drives it (DESIGN.md §12).

Create a keep from the launcher, scan it with F5, search it, rescan after changes, open a
keep from the command line, and scan a root that is offline.
"""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from pytestqt.qtbot import QtBot

from tagalot.core.keep import DEFAULT_EXCLUDES, RootConfig, ThemeRef, create_keep
from tagalot.core.settings import Settings, load_settings
from tagalot.ui import app as app_module
from tagalot.ui.app import TagalotApp
from tagalot.ui.launcher import LauncherDialog, NewKeepDialog
from tagalot.ui.main_window import MainWindow
from tagalot.ui.opening import open_keep_async
from tagalot.ui.search_view import SearchPage

pytestmark = pytest.mark.gui

FILES = {
    "Iceland/glacier.jpg": b"jpg" * 10,
    "Iceland/geyser.jpg": b"jpg" * 20,
    "Iceland 2024/northern lights.png": b"png" * 30,
    "Documents/März.txt": b"Latte 3.40",
    "Documents/notes.txt": b"coffee, film",
    "readme.md": b"# Photos",
    # Leftovers the default excludes skip.
    ".DS_Store": b"Bud1",
    "Iceland/._glacier.jpg": b"Mac OS X",
    "Iceland/Thumbs.db": b"\xd0\xcf",
}
SCANNED = 6


def _make_files(folder: Path) -> Path:
    for relpath, data in FILES.items():
        path = folder / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return folder


def _page(window: MainWindow) -> SearchPage:
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    return page


def _scan(qtbot: QtBot, window: MainWindow) -> str:
    """Press F5 and wait for the scan; returns the status bar's summary."""
    window.activateWindow()
    with qtbot.waitSignal(window.scans.finished, timeout=20_000):
        QTest.keyClick(window, Qt.Key.Key_F5)
    return window.statusBar().currentMessage()


def _search(qtbot: QtBot, page: SearchPage, text: str, expected: str) -> None:
    page.filter_bar.text_edit.setText(text)
    QTest.keyClick(page.filter_bar.text_edit, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: page.status.text() == expected, timeout=5000)


def test_create_a_keep_scan_it_and_search_it(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _make_files(tmp_path / "My Photos")
    tagalot = TagalotApp(Settings(), tmp_path / "settings.toml")
    launcher = tagalot.show_launcher()
    qtbot.addWidget(launcher)

    def fill_in(self: NewKeepDialog) -> QDialog.DialogCode:
        self.name_edit.setText("Photos")
        self.location_edit.setText(str(tmp_path))
        self.root_edit.setText(str(files))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(NewKeepDialog, "exec", fill_in)
    qtbot.waitUntil(lambda: launcher.catalog is not None, timeout=10_000)
    launcher.new_button.click()
    qtbot.waitUntil(lambda: len(tagalot.windows) == 1, timeout=10_000)
    window = tagalot.windows[0]
    assert window.windowTitle() == "Photos — Tagalot"
    page = _page(window)
    qtbot.waitUntil(lambda: page.status.text() == "Nothing found", timeout=5000)

    summary = _scan(qtbot, window)
    assert summary == f"Scan finished: {SCANNED} new, 0 changed, 0 missing."
    qtbot.waitUntil(lambda: page.status.text() == f"{SCANNED} items", timeout=5000)
    titles = {page.model.index(r, 0).data() for r in range(page.model.rowCount())}
    assert titles == {"glacier.jpg", "geyser.jpg", "northern lights.png", "März.txt"} | {
        "notes.txt",
        "readme.md",
    }  # no .DS_Store, ._glacier.jpg, or Thumbs.db

    _search(qtbot, page, "gla", "1 item")
    _search(qtbot, page, "märz", "1 item")
    _search(qtbot, page, "zebra", "Nothing found")
    _search(qtbot, page, "", f"{SCANNED} items")

    # Closing the window closes the keep, so it can be moved or deleted.
    session = window.session
    assert session is not None
    window.close()
    qtbot.waitUntil(lambda: session.closed, timeout=10_000)
    assert tagalot.windows == []
    moved = tmp_path / "Photos moved.keep"
    (tmp_path / "Photos.keep").rename(moved)  # Windows refuses this while the keep is open
    assert (moved / "keep.db").exists()


def test_a_rescan_finds_changes(qtbot: QtBot, tmp_path: Path) -> None:
    files = _make_files(tmp_path / "files")
    roots = [RootConfig("r", "Files", str(files), list(DEFAULT_EXCLUDES))]
    keep = create_keep(tmp_path / "Files.keep", "Files", ThemeRef("generic", 1), roots)
    tagalot = TagalotApp(Settings(), tmp_path / "settings.toml")
    opened: list[MainWindow] = []
    open_keep_async(
        None,
        keep.dir,
        tagalot.settings,
        lambda s: opened.append(tagalot.show_keep(s)),
        settings_path=tmp_path / "settings.toml",
    )
    qtbot.waitUntil(lambda: bool(opened), timeout=10_000)
    window = opened[0]
    qtbot.addWidget(window)
    page = _page(window)
    _scan(qtbot, window)
    qtbot.waitUntil(lambda: page.status.text() == f"{SCANNED} items", timeout=5000)

    (files / "Iceland" / "waterfall.jpg").write_bytes(b"new")
    (files / "readme.md").write_bytes(b"# Photos, updated and longer")
    stamp = os.stat(files / "readme.md").st_mtime + 5
    os.utime(files / "readme.md", (stamp, stamp))
    (files / "Documents" / "notes.txt").unlink()
    assert _scan(qtbot, window) == "Scan finished: 1 new, 1 changed, 1 missing."
    # Missing isn't deleted: the notes entity stays (DESIGN.md §4), and the new file is found.
    qtbot.waitUntil(lambda: page.status.text() == f"{SCANNED + 1} items", timeout=5000)
    _search(qtbot, page, "waterfall", "1 item")
    tagalot.close_all()


def test_an_offline_root_is_reported_not_emptied(qtbot: QtBot, tmp_path: Path) -> None:
    share = tmp_path / "share"  # doesn't exist yet: the root is offline
    roots = [RootConfig("nas", "NAS", str(share), list(DEFAULT_EXCLUDES))]
    keep = create_keep(tmp_path / "Nas.keep", "Nas", ThemeRef("generic", 1), roots)
    tagalot = TagalotApp(Settings(), tmp_path / "settings.toml")
    opened: list[MainWindow] = []
    open_keep_async(
        None,
        keep.dir,
        tagalot.settings,
        lambda s: opened.append(tagalot.show_keep(s)),
        settings_path=tmp_path / "settings.toml",
    )
    qtbot.waitUntil(lambda: bool(opened), timeout=10_000)
    window = opened[0]
    qtbot.addWidget(window)
    page = _page(window)

    assert _scan(qtbot, window) == "Scan finished: 0 new, 0 changed, 0 missing. Offline: nas."
    qtbot.waitUntil(lambda: page.status.text() == "Nothing found", timeout=5000)
    _make_files(share)  # the share comes back
    assert _scan(qtbot, window) == f"Scan finished: {SCANNED} new, 0 changed, 0 missing."
    qtbot.waitUntil(lambda: page.status.text() == f"{SCANNED} items", timeout=5000)
    tagalot.close_all()


def test_the_command_line_opens_a_keep(
    qapp: QApplication, tmp_path: Path, private_user_folders: Path
) -> None:
    files = _make_files(tmp_path / "files")
    roots = [RootConfig("r", "Files", str(files), list(DEFAULT_EXCLUDES))]
    keep = create_keep(tmp_path / "Files.keep", "Files", ThemeRef("generic", 1), roots)
    seen: list[str] = []

    def quit_once_open() -> None:
        windows = [w for w in qapp.topLevelWidgets() if isinstance(w, MainWindow)]
        if any(w.isVisible() for w in windows):
            seen.extend(w.windowTitle() for w in windows if w.isVisible())
            qapp.exit(0)
        else:
            QTimer.singleShot(50, quit_once_open)

    QTimer.singleShot(0, quit_once_open)
    assert app_module.run(["tagalot", str(keep.dir)]) == 0
    assert seen == ["Files — Tagalot"]
    # Remembered as a recent keep, in the test's private settings file, not the real one.
    assert (private_user_folders / "settings.toml").exists()
    assert load_settings().recent_keeps == [keep.dir]


def test_the_command_line_falls_back_to_the_launcher(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    seen: list[str] = []

    def quit_once_launcher_shows() -> None:
        dialogs = [w for w in qapp.topLevelWidgets() if w.isVisible() and w.windowTitle()]
        if warnings and any(isinstance(w, LauncherDialog) for w in dialogs):
            seen.extend(w.windowTitle() for w in dialogs)
            qapp.exit(0)
        else:
            QTimer.singleShot(50, quit_once_launcher_shows)

    QTimer.singleShot(0, quit_once_launcher_shows)
    assert app_module.run(["tagalot", str(tmp_path / "Missing.keep")]) == 0
    assert "does not exist" in warnings[0]
    assert "Tagalot" in seen
