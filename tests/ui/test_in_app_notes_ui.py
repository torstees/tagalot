"""Notes typed in Tagalot and Save as note… in the window (#383): Add a note and ✎ on an
item's page, Ctrl+Enter saves and Esc cancels, one undo step; a note file's note is
read-only; Save as note… writes the note into a writable notes folder and the scan reads
it; and Keep configuration's writable box and layout."""

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolButton
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.keep import load_keep_config
from tagalot.core.models import Entity, EntityNote
from tagalot.core.save_note import NewNote
from tagalot.core.session import KeepSession
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tests.ui.test_contents_search import _open
from tests.ui.test_notes_folder_ui import _config, session, window

__all__ = ["session", "window"]

pytestmark = pytest.mark.gui


def _body(session: KeepSession, title: str) -> str | None:
    with session.reader.connect() as conn:
        return conn.scalar(
            select(EntityNote.body)
            .join(Entity, Entity.id == EntityNote.entity_id)
            .where(Entity.title == title)
        )


def _double_click(widget: object) -> None:
    point = QPointF(5, 5)
    event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        point,
        point,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)  # type: ignore[arg-type]


def _shown(qtbot: QtBot, page: DetailPage, text: str) -> None:
    note = page.note_view
    qtbot.waitUntil(
        lambda: text in note.body.toPlainText() and note.body.isVisible(),
        timeout=5000,
    )


def test_typing_a_note_on_the_page(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    session.scan_all()
    page = _open(qtbot, window, session, "Robert De Niro")
    note = page.note_view
    qtbot.waitUntil(note.add_button.isVisible, timeout=5000)
    assert not note.pencil.isVisible()
    assert not note.body.isVisible()
    assert not page.save_note_action.isVisible()  # no writable notes folder
    note.add_button.click()
    assert note.editing()
    assert note.box.hasFocus() or not QApplication.focusWindow()
    note.box.setPlainText("Born in **Greenwich Village**.")
    QTest.keyClick(note.box, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert not note.editing()
    qtbot.waitUntil(lambda: _body(session, "Robert De Niro") is not None, timeout=5000)
    assert _body(session, "Robert De Niro") == "Born in **Greenwich Village**."
    _shown(qtbot, page, "Born in Greenwich Village.")
    assert note.pencil.isVisible()
    assert not note.add_button.isVisible()
    # The note is in the keep a moment before its undo step is recorded.
    _undo_label(qtbot, session, "Add a note to 'Robert De Niro'")

    # Esc cancels; Enter alone is a new line.
    _double_click(note.body.viewport())
    assert note.editing()
    QTest.keyClick(note.box, Qt.Key.Key_Return)
    assert note.editing()
    QTest.keyClick(note.box, Qt.Key.Key_Escape)
    assert not note.editing()
    assert session.tags.undo_label == "Add a note to 'Robert De Niro'"

    # Emptying it removes it.
    note.pencil.click()
    note.box.clear()
    save = note.findChild(QToolButton, "save_note")
    assert save is not None
    save.click()
    qtbot.waitUntil(lambda: _body(session, "Robert De Niro") is None, timeout=5000)
    qtbot.waitUntil(note.add_button.isVisible, timeout=5000)
    _undo_label(qtbot, session, "Remove the note of 'Robert De Niro'")
    session.tags.undo()
    assert _body(session, "Robert De Niro") == "Born in **Greenwich Village**."


def _undo_label(qtbot: QtBot, session: KeepSession, expected: str) -> None:
    def recorded() -> None:
        assert session.tags.undo_label == expected

    qtbot.waitUntil(recorded, timeout=5000)


def test_a_note_files_note_is_read_only(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    session.set_notes_folder("movies", "Notes")
    session.set_notes_writing(writable=True)
    session.scan_all()
    page = _open(qtbot, window, session, "Al Pacino")
    _shown(qtbot, page, "Born in East Harlem.")
    note = page.note_view
    assert note.open_button.isVisible()
    assert not note.pencil.isVisible()
    assert not note.add_button.isVisible()
    assert not page.save_note_action.isVisible()  # it has a note file
    _double_click(note.body.viewport())
    assert not note.editing()


def test_save_as_note(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session.set_notes_folder("movies", "Notes")
    session.scan_all()
    page = _open(qtbot, window, session, "Robert De Niro")
    qtbot.waitUntil(page.note_view.add_button.isVisible, timeout=5000)
    assert not page.save_note_action.isVisible()  # the notes folder isn't writable
    session.set_notes_writing(writable=True)
    session.tags.edit_note(page.entity_id, "Heat, 1995.")
    page.refresh()
    _shown(qtbot, page, "Heat, 1995.")
    assert page.save_note_action.isVisible()
    asked: list[NewNote] = []

    def confirm(self: MainWindow, note: NewNote) -> bool:
        asked.append(note)
        return True

    monkeypatch.setattr(MainWindow, "confirm_save_note", confirm)
    page.save_note_action.trigger()
    target = tmp_path / "movie-files" / "Notes" / "Actors" / "Robert De Niro.md"
    qtbot.waitUntil(target.exists, timeout=5000)
    assert [n.relpath for n in asked] == ["Notes/Actors/Robert De Niro.md"]
    assert target.read_text("utf-8") == "---\ntitle: Robert De Niro\n---\n\nHeat, 1995.\n"
    # The scan that follows reads it: the note is the file's now.
    qtbot.waitUntil(lambda: page.note_view.open_button.isVisible(), timeout=10000)
    assert not page.note_view.pencil.isVisible()
    assert not page.save_note_action.isVisible()

    # Saving again over a file that is there is refused, and the file stays.
    session.set_notes_folder(None)
    session.scan_all()
    session.tags.edit_note(page.entity_id, "Typed again.")
    session.set_notes_folder("movies", "Notes")
    session.set_notes_writing(writable=True)
    warned: list[str] = []
    monkeypatch.setattr(
        "tagalot.ui.main_window.QMessageBox.warning",
        lambda parent, title, text: warned.append(text),
    )
    window.save_as_note(page.entity_id)
    qtbot.waitUntil(lambda: bool(warned), timeout=5000)
    assert "Notes/Actors/Robert De Niro.md is there already" in warned[0]
    assert target.read_text("utf-8").endswith("Heat, 1995.\n")


def test_letting_save_as_note_write(
    qtbot: QtBot, window: MainWindow, session: KeepSession, tmp_path: Path
) -> None:
    config = _config(qtbot, window, tmp_path)
    assert not config.notes_writable.isEnabled()
    assert not config.new_notes.isEnabled()
    config.use_notes_folder(str(tmp_path / "movie-files" / "Notes"))
    qtbot.waitUntil(config.notes_writable.isEnabled, timeout=5000)
    keep_toml = session.keep.dir / "keep.toml"
    config.ask_notes_writable = lambda: False  # type: ignore[method-assign]
    config.notes_writable.click()
    assert not config.notes_writable.isChecked()
    assert not load_keep_config(keep_toml).notes_writable
    config.ask_notes_writable = lambda: True  # type: ignore[method-assign]
    config.notes_writable.click()
    qtbot.waitUntil(lambda: load_keep_config(keep_toml).notes_writable, timeout=5000)
    config.new_notes.setCurrentIndex(1)
    config.new_notes.activated.emit(1)
    qtbot.waitUntil(lambda: load_keep_config(keep_toml).new_notes == "one-folder", timeout=5000)
    config.no_notes_folder.click()
    qtbot.waitUntil(lambda: load_keep_config(keep_toml).notes_root is None, timeout=5000)
    qtbot.waitUntil(lambda: not config.notes_writable.isEnabled(), timeout=5000)
    assert not config.notes_writable.isChecked()
    config.close()


def test_letting_write_to_file_change_notes(
    qtbot: QtBot, window: MainWindow, session: KeepSession, tmp_path: Path
) -> None:
    """Its own checkbox, asking first (#501)."""
    config = _config(qtbot, window, tmp_path)
    assert not config.notes_write_back.isEnabled()
    config.use_notes_folder(str(tmp_path / "movie-files" / "Notes"))
    qtbot.waitUntil(config.notes_write_back.isEnabled, timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)  # that save done first
    keep_toml = session.keep.dir / "keep.toml"
    config.ask_notes_write_back = lambda: False  # type: ignore[method-assign]
    config.notes_write_back.click()
    assert not config.notes_write_back.isChecked()
    config.ask_notes_write_back = lambda: True  # type: ignore[method-assign]
    config.notes_write_back.click()
    # On the session, not by reading keep.toml: on Windows a read can block the save.
    qtbot.waitUntil(lambda: session.keep.config.notes_write_back, timeout=5000)
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)
    saved = load_keep_config(keep_toml)
    assert saved.notes_write_back
    assert not saved.notes_writable  # Save as note… still off
    config.close()
