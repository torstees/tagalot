"""Write to file… in the window (#299), on the books demo: marking a folder writable, the
preview, writing a Markdown book's front matter, and reading it back."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.keep import load_keep_config
from tagalot.core.models import Entity
from tagalot.core.root_admin import edit_root
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.writeback import FileWrite, WritePlan
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.workers import ScanController
from tagalot.ui.write_back_dialog import WriteBackDialog, front_matter_diff

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
MOL = "Royal Road/Mother of Learning 01.md"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_books_demo(tmp_path), Settings()) as session:
        yield session


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()
    qtbot.waitUntil(lambda: window.tag_panel.loaded, timeout=5000)
    return window


def _id(session: KeepSession, title: str) -> int:
    """A book's id (Mother of Learning is a series too)."""
    with session.reader.connect() as conn:
        found = conn.scalar(
            select(Entity.id).where(Entity.title == title, Entity.type == "books.book")
        )
    assert found is not None
    return found


def _writable(session: KeepSession) -> None:
    session.save_config(edit_root(session.keep.config, session.keep.dir, "books", writable=True))


def _page(qtbot: QtBot, window: MainWindow, title: str) -> DetailPage:
    assert window.session is not None
    window.open_entity(_id(window.session, title))
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: page.detail is not None, timeout=5000)
    return page


def _md(session: KeepSession) -> Path:
    return Path(session.root_path("books")) / MOL


def test_a_folder_is_made_writable_after_asking(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = window.configure_keep()
    qtbot.waitUntil(lambda: "files and folders" in config.status.text(), timeout=5000)
    assert not config.writable.isChecked()
    answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: answers.pop(0))
    config.writable.click()  # No: stays off
    assert not config.writable.isChecked()
    assert not load_keep_config(session.keep.toml_path).roots[0].writable
    with qtbot.waitSignal(config.changed, timeout=5000):
        config.writable.click()  # Yes
    assert load_keep_config(session.keep.toml_path).roots[0].writable
    assert "writable = true" in session.keep.toml_path.read_text(encoding="utf-8")
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)


def test_write_to_file_from_an_items_page(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writable(session)
    mol = _id(session, "Mother of Learning")
    read = next(
        t for t in session.tag_cache.get() if session.tag_cache.get().node(t).name == "Read"
    )
    session.tags.apply([mol], [read])
    session.tags.edit_field(mol, "publisher", "Self-published")
    before = _md(session).read_text(encoding="utf-8")
    shown: list[WritePlan] = []

    def confirm(plan: WritePlan) -> list[FileWrite]:
        shown.append(plan)
        return [f for f in plan.files if f.changes]

    monkeypatch.setattr(window, "confirm_write_back", confirm)
    messages: list[str] = []
    window.statusBar().messageChanged.connect(messages.append)
    page = _page(qtbot, window, "Mother of Learning")
    action = page.findChild(QAction, "write_back")
    assert action is not None
    action.trigger()
    qtbot.waitUntil(lambda: any(m.startswith("Wrote 1 file.") for m in messages), timeout=5000)
    [write] = shown[0].files
    assert write.relpath == MOL
    assert "tags: [Fantasy, time-loop, Read]" in write.after
    assert "publisher: Self-published" in write.after
    after = _md(session).read_text(encoding="utf-8")
    assert after == before.replace(write.before, write.after)  # the block, and only it
    [wrote] = [m for m in messages if m.startswith("Wrote")]
    assert "Copies of the originals are in backups/" in wrote
    assert (session.keep.dir / "backups").is_dir()
    # Tagalot then reads what it wrote: nothing more to write.
    qtbot.waitUntil(lambda: not window.scans.running, timeout=10000)
    qtbot.waitUntil(lambda: session.has_keywords, timeout=5000)
    shown.clear()
    infos: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: infos.append(a[2]))
    messages.clear()
    action.trigger()
    qtbot.waitUntil(lambda: any(m.startswith("Nothing to write") for m in messages), timeout=5000)
    assert not shown
    assert not infos


def test_nothing_is_written_in_a_folder_that_doesnt_allow_it(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _md(session).read_bytes()
    infos: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: infos.append(a[2]))
    window.write_back([_id(session, "Mother of Learning"), _id(session, "Good Omens")])
    qtbot.waitUntil(lambda: bool(infos), timeout=5000)
    assert (
        "Mother of Learning (Royal Road/Mother of Learning 01.md): its folder doesn't" in infos[0]
    )
    assert "Good Omens: it has no Markdown file" in infos[0]
    assert _md(session).read_bytes() == before


def test_the_preview_dialog(qtbot: QtBot) -> None:
    def write(relpath: str, **values: object) -> FileWrite:
        return FileWrite(1, "A Story", 2, "r", relpath, "", 1, 1, **values)  # type: ignore[arg-type]

    plan = WritePlan(
        [
            write("a.md", before="title: A\n", after="title: A\ntags: [Read]\n", text=b"x"),
            write("b.md", problem="it changed since Tagalot last read it"),
            write("c.md", before="title: C\n", after="title: C\n"),
        ],
        [("Good Omens", "it has no Markdown file")],
    )
    dialog = WriteBackDialog(plan)
    qtbot.addWidget(dialog)
    ok = dialog.buttons.button(dialog.buttons.StandardButton.Ok)
    assert ok.text() == "Write 1 file"
    assert dialog.preview.toPlainText() == " title: A\n+tags: [Read]"
    assert front_matter_diff(plan.files[1]) == "Not written: it changed since Tagalot last read it."
    assert front_matter_diff(plan.files[2]).startswith("Nothing to change")
    assert dialog.skipped.text() == "Not written: Good Omens (it has no Markdown file)."
    assert dialog.chosen() == [plan.files[0]]
    dialog.files.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert dialog.chosen() == []
    assert not ok.isEnabled()
    assert dialog.files.item(1).flags() & Qt.ItemFlag.ItemIsUserCheckable == Qt.ItemFlag(0)
