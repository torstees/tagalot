"""Export tags… and Import tags… on the Tag manager (#395)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QMessageBox, QWidget
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import PATH_SEPARATOR
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.tag_import import TagImportDialog
from tagalot.ui.tag_manager import TagManagerPage
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_demo(tmp_path), Settings()) as session:
        yield session


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()
    window.navigation.select(NavTarget("tags", label="Tag manager"))
    qtbot.waitUntil(lambda: _page(window).loaded, timeout=5000)
    return window


def _page(window: MainWindow) -> TagManagerPage:
    page = window.stack.currentWidget()
    assert isinstance(page, TagManagerPage)
    return page


def _paths(session: KeepSession) -> set[str]:
    tree = session.tag_cache.get()
    return {PATH_SEPARATOR.join(tree.path(t)) for t in tree}


def _wait_status(qtbot: QtBot, window: MainWindow, prefix: str) -> None:
    def check() -> None:
        assert window.statusBar().currentMessage().startswith(prefix), (
            window.statusBar().currentMessage()
        )

    qtbot.waitUntil(check, timeout=5000)


def test_export_saves_the_tree(
    qtbot: QtBot, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out" / "tags.yaml"
    target.parent.mkdir()
    asked: list[str] = []

    def choose(name: str) -> str:
        asked.append(name)
        return str(target)

    monkeypatch.setattr(window, "choose_export_path", choose)
    _page(window).export_button.click()
    qtbot.waitUntil(target.exists, timeout=5000)
    assert asked == ["Demo tags.yaml"]
    text = target.read_text(encoding="utf-8")
    assert "    Iceland:\n      description: Summer 2019 road trip" in text
    assert "aliases: [Ísland]" in text
    assert "\nFavorites:\n" in text


def test_import_previews_then_imports_as_one_step(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "more tags.yaml"
    source.write_text(
        "Places:\n  tags:\n    Norway: [Oslo]\n    ísland:\n      color: '#2e86de'\n"
        "People:\n  color: nope\n",
        encoding="utf-8",
    )
    before = _paths(session)
    seen: list[tuple[str, str, bool]] = []

    def review(dialog: TagImportDialog) -> bool:
        seen.append((dialog.summary.text(), dialog.report.toPlainText(), dialog.delete_unused()))
        dialog.delete_box.setChecked(True)
        seen.append((dialog.summary.text(), dialog.report.toPlainText(), dialog.delete_unused()))
        dialog.delete_box.setChecked(False)
        return dialog.import_button.isEnabled()

    monkeypatch.setattr(window, "choose_tag_file", lambda: str(source))
    monkeypatch.setattr(window, "review_tag_import", review)
    _page(window).import_button.click()
    _wait_status(qtbot, window, "Imported tags: 2 new, 1 updated.")

    (summary, report, deleting), (_, with_delete, _) = seen
    assert summary == "2 new tags, 1 updated, 1 problem."
    assert not deleting
    assert "Line 7: 'nope' isn't a color" in report
    assert f"Places{PATH_SEPARATOR}Norway{PATH_SEPARATOR}Oslo" in report
    assert f"Places{PATH_SEPARATOR}Iceland: color" in report  # matched by its alias
    assert "Kept, since items use them (" in with_delete
    assert "Kept, since items use them" not in report

    assert _paths(session) == before | {
        f"Places{PATH_SEPARATOR}Norway",
        f"Places{PATH_SEPARATOR}Norway{PATH_SEPARATOR}Oslo",
    }
    assert window.tag_actions.undo_label == "Import tags from 'more tags.yaml'"
    window.tag_actions.undo()
    qtbot.waitUntil(lambda: _paths(session) == before, timeout=5000)


def test_a_file_that_isnt_yaml_cant_be_imported(
    qtbot: QtBot, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "broken.yaml"
    source.write_text("Places: [Iceland\n", encoding="utf-8")
    seen: list[TagImportDialog] = []

    def review(dialog: TagImportDialog) -> bool:
        seen.append(dialog)
        return False

    monkeypatch.setattr(window, "choose_tag_file", lambda: str(source))
    monkeypatch.setattr(window, "review_tag_import", review)
    _page(window).import_button.click()
    qtbot.waitUntil(lambda: bool(seen), timeout=5000)
    [dialog] = seen
    assert not dialog.import_button.isEnabled()
    assert not dialog.delete_box.isEnabled()
    assert dialog.report.toPlainText().startswith("Problems, left out (1)\n  Line 2: ")


def test_a_file_that_isnt_text_is_reported(
    qtbot: QtBot, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "picture.yaml"
    source.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x80\x81")
    warnings: list[str] = []

    def warn(parent: QWidget, title: str, text: str) -> QMessageBox.StandardButton:
        warnings.append(text)
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(window, "choose_tag_file", lambda: str(source))
    monkeypatch.setattr(QMessageBox, "warning", warn)
    _page(window).import_button.click()
    qtbot.waitUntil(lambda: bool(warnings), timeout=5000)
    assert warnings == ["Couldn't read picture.yaml: it isn't a text file."]
