"""Take the documentation site's screenshots (docs/images/screens), from the demo keeps.

    uv run python scripts/make_screenshots.py

builds the demos in a temporary folder (``scratch/`` is left alone), opens each in a window
drawn offscreen at one size, puts it in the state each picture needs, and saves the window.
Run it after a change to how the window looks, and commit the pictures. On Windows the
offscreen window uses the system fonts (``C:\\Windows\\Fonts``); elsewhere, the fonts
Qt finds (fontconfig).
"""

import importlib.util
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32" and "QT_QPA_FONTDIR" not in os.environ:
    os.environ["QT_QPA_FONTDIR"] = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

from PySide6.QtCore import QItemSelectionModel, QThreadPool
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.themes.loader import load_themes
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.launcher import NewKeepDialog
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import ScanController

ROOT = Path(__file__).resolve().parents[1]
SCREENS = ROOT / "docs" / "images" / "screens"
WIDTH, HEIGHT = 1200, 720
"""Every window's size, so the pictures line up on the site."""


def demo_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_demo_keep", ROOT / "scripts" / "make_demo_keep.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def settle(app: QApplication, seconds: float = 1.5) -> None:
    """Let workers finish and the window repaint."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.03)


SHOWN_FOLDER = r"D:\Keeps"
"""Shown instead of the temporary folder the demos are built in: the pictures are
published, and that folder's path names this computer's user."""
built_in: Path | None = None


def save(widget: QWidget, name: str) -> None:
    """Save a picture of ``widget``, any label naming the demos' temporary folder showing
    :data:`SHOWN_FOLDER` instead."""
    if built_in is not None:
        for label in widget.findChildren(QLabel):
            if str(built_in) in label.text():
                label.setText(label.text().replace(str(built_in), SHOWN_FOLDER))
    widget.grab().save(str(SCREENS / f"{name}.png"))
    print(f"docs/images/screens/{name}.png")


def select_title(page: SearchPage, title: str) -> int:
    """Select the result called ``title`` in a page's list (or grid: they share it), and
    return its item's id."""
    for row in range(page.model.rowCount()):
        hit = page.model.hit(row)
        if hit is not None and hit.title == title:
            page.table.selectionModel().select(
                page.model.index(row, 0),
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
            return hit.id
    raise LookupError(title)


def window_for(app: QApplication, keep: Path) -> tuple[KeepSession, MainWindow]:
    session = KeepSession.open(keep, Settings())
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    window.resize(WIDTH, HEIGHT)
    window.show()
    settle(app)
    return session, window


def page(window: MainWindow) -> SearchPage:
    current = window.stack.currentWidget()
    assert isinstance(current, SearchPage)
    return current


def shoot(app: QApplication, keep: Path, steps: Callable[[MainWindow, KeepSession], None]) -> None:
    session, window = window_for(app, keep)
    try:
        steps(window, session)
    finally:
        window.close()
        settle(app, 0.3)
        session.close()


def music(app: QApplication, keep: Path) -> None:
    def steps(window: MainWindow, session: KeepSession) -> None:
        window.navigation.select(NavTarget("view", key="Browse", label="Browse"))
        settle(app)
        browse = page(window)
        browse.tree.expandToDepth(0)
        settle(app)
        save(window, "music-browse")

        window.navigation.select(NavTarget("view", key="Songs", label="Songs"))
        settle(app)
        songs = page(window)
        tree = session.tag_cache.get()
        mood = tree.find_child(None, "Mood")
        calm = tree.find_child(mood, "Calm") if mood is not None else None
        if calm is not None:
            songs.filter_bar.add_tag(calm)
        songs.filter_bar.set_text("blue")
        settle(app)
        save(window, "searching")

        window.navigation.select(NavTarget("triage", label="Triage"))
        settle(app)
        save(window, "triage")

    shoot(app, keep, steps)


def assets(app: QApplication, keep: Path) -> None:
    def steps(window: MainWindow, session: KeepSession) -> None:
        window.navigation.select(NavTarget("view", key="Assets", label="Assets"))
        settle(app, 3)
        grid = page(window)
        grid.table.selectRow(0)
        settle(app, 2)
        save(window, "assets-grid")

    shoot(app, keep, steps)


def books(app: QApplication, keep: Path) -> None:
    def steps(window: MainWindow, session: KeepSession) -> None:
        settle(app, 2)
        save(window, "dashboard")
        window.navigation.select(NavTarget("view", key="Books", label="Books"))
        settle(app, 2)
        colour = select_title(page(window), "The Colour of Magic")
        settle(app)
        window.open_entity(colour)
        settle(app, 2)
        detail = window.stack.currentWidget()
        if isinstance(detail, DetailPage):  # room for its details over its credits
            detail.splitter.setSizes([440, 180])
        settle(app, 1)
        save(window, "item-page")

    shoot(app, keep, steps)


def research(app: QApplication, keep: Path) -> None:
    def steps(window: MainWindow, session: KeepSession) -> None:
        window.navigation.select(NavTarget("view", key="Papers", label="Papers"))
        settle(app)
        papers = page(window)
        papers.filter_bar.contents_box.setChecked(True)
        papers.filter_bar.set_text("recurrence")
        settle(app)
        select_title(papers, "Attention Is All You Need")
        settle(app)
        save(window, "in-documents")

        config = window.configure_keep()
        config.resize(820, 560)
        config.tabs.setCurrentIndex(config.tabs.count() - 1)
        settle(app)
        save(config, "keep-configuration")
        config.close()

    shoot(app, keep, steps)


def launcher(app: QApplication) -> None:
    dialog = NewKeepDialog(load_themes())
    dialog.resize(560, 0)
    dialog.name_edit.setText("Papers")
    dialog.location_edit.setText(r"D:\Keeps")  # not this computer's own folders
    dialog.root_edit.setText(r"\\nas\papers")
    dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findText("Research"))
    dialog.show()
    settle(app, 0.5)
    save(dialog, "new-keep")
    dialog.close()


def main() -> int:
    app = QApplication(sys.argv[:1])
    SCREENS.mkdir(parents=True, exist_ok=True)
    script = demo_script()
    with tempfile.TemporaryDirectory(prefix="tagalot-screens-") as folder:
        base = Path(folder)
        global built_in
        built_in = base
        music(app, script.make_music_demo(base))
        assets(app, script.make_assets_demo(base))
        books(app, script.make_books_demo(base))
        research(app, script.make_research_demo(base))
    launcher(app)
    return 0


if __name__ == "__main__":
    sys.exit(main())
