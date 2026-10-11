"""The notes folder in the window (#382): chosen in Keep configuration (inside a watched
folder, or watched for notes only), its notes on items' pages, and Triage → Notes."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from pytestqt.qtbot import QtBot

from tagalot.core.handlers import OPEN, REVEAL, FileToOpen
from tagalot.core.keep import load_keep_config
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.keep_config import KeepConfigWindow
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.triage import NOTES_TAB, TriagePage
from tagalot.ui.workers import ScanController
from tests.ui.test_contents_search import SCRIPT, _open

pytestmark = pytest.mark.gui

PACINO = """---
born: 1940
award: Oscar
---
Born in East Harlem.
"""


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keep = module.make_movies_demo(tmp_path, notes=False)  # each test chooses its own
    notes = tmp_path / "movie-files" / "Notes"
    (notes / "Actors").mkdir(parents=True)
    (notes / "Actors" / "Al Pacino.md").write_text(PACINO, encoding="utf-8")
    (notes / "Nobody.md").write_text("Who is this?\n", encoding="utf-8")
    with KeepSession.open(keep, Settings()) as session:
        yield session


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> Iterator[MainWindow]:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()
    yield window
    window.thumbnails.clear()
    window.thumbnails.wait()


def _config(qtbot: QtBot, window: MainWindow, tmp_path: Path) -> KeepConfigWindow:
    config = window.configure_keep()
    config.settings_path = tmp_path / "settings.toml"
    config.ask_scan = lambda root: False  # type: ignore[method-assign]
    return config


def test_choosing_the_notes_folder(
    qtbot: QtBot, window: MainWindow, session: KeepSession, tmp_path: Path
) -> None:
    config = _config(qtbot, window, tmp_path)
    assert config.notes_folder.text() == "None"
    assert not config.no_notes_folder.isEnabled()

    inside = tmp_path / "movie-files" / "Notes"
    with qtbot.waitSignal(config.changed, timeout=5000) as saved:
        config.use_notes_folder(str(inside))
    assert "next scan of Movie files reads it" in saved.args[0]
    loaded = load_keep_config(session.keep.toml_path)
    assert (loaded.notes_root, loaded.notes_path) == ("movies", "Notes")
    assert config.notes_folder.text() == f"{inside}\n(in Movie files: Notes)"

    elsewhere = tmp_path / "Actor notes"
    elsewhere.mkdir()
    config.ask_notes_root = lambda folder: False  # type: ignore[method-assign]
    config.use_notes_folder(str(elsewhere))  # declined: nothing changes
    assert load_keep_config(session.keep.toml_path).notes_path == "Notes"
    config.ask_notes_root = lambda folder: True  # type: ignore[method-assign]
    with qtbot.waitSignal(config.changed, timeout=5000) as watched:
        config.use_notes_folder(str(elsewhere))
    assert watched.args[0] == f"Now watching {elsewhere} for notes only."
    loaded = load_keep_config(session.keep.toml_path)
    assert [r.path for r in loaded.roots][1] == str(elsewhere)
    assert (loaded.notes_root, loaded.notes_path) == (loaded.roots[1].id, "")

    with qtbot.waitSignal(config.changed, timeout=5000):
        config.no_notes_folder.click()
    assert load_keep_config(session.keep.toml_path).notes_root is None
    assert config.notes_folder.text() == "None"
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)


def test_notes_on_pages_and_in_triage(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.set_notes_folder("movies", "Notes")
    session.scan_all()
    page = _open(qtbot, window, session, "Al Pacino")
    qtbot.waitUntil(page.note_view.isVisible, timeout=5000)
    assert "Born in East Harlem." in page.note_view.body.toPlainText()
    assert page.open_note_action.isEnabled()

    opened: list[tuple[FileToOpen, str]] = []
    monkeypatch.setattr(FileOpener, "act", lambda self, file, how: opened.append((file, how)))
    window.navigation.select(NavTarget("triage", label="Triage"))
    triage = window.stack.currentWidget()
    assert isinstance(triage, TriagePage)
    qtbot.waitUntil(lambda: triage.tabs.tabText(NOTES_TAB) == "Notes (1)", timeout=5000)
    triage.tabs.setCurrentIndex(NOTES_TAB)
    assert [n.relpath for n in triage.notes.notes] == ["Notes/Nobody.md"]
    assert triage.notes.data(triage.notes.index(0, 1)) == (
        "No item has this title or one of its aliases"
    )
    assert triage.files.files == [] or all(
        not f.relpath.startswith("Notes/") for f in triage.files.files
    )
    triage.notes_table.selectRow(0)
    triage._open_note(OPEN)
    triage._open_note(REVEAL)
    assert [(f.path.endswith("Nobody.md"), how) for f, how in opened] == [
        (True, OPEN),
        (True, REVEAL),
    ]
