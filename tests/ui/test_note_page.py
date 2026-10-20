"""An item's note on its page (#380, DESIGN.md §9 *Notes*, §12): the body rendered under
the title, capped with Show more, its pictures and links from the note's folder; Open
note; and Extra fields from the note, marked, editable, and removable for good."""

import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QThreadPool, QUrl
from PySide6.QtGui import QAction, QFocusEvent, QImage, QTextDocument
from PySide6.QtWidgets import QApplication, QToolButton, QWidget
from pytestqt.qtbot import QtBot

from tagalot.core.handlers import OPEN, FileToOpen
from tagalot.core.keep import load_keep_config
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings, load_settings
from tagalot.ui import field_editor
from tagalot.ui.detail_view import IGNORE_KEY, DetailPage
from tagalot.ui.field_editor import EditableValue
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.main_window import MainWindow
from tagalot.ui.workers import ScanController
from tests.ui.test_contents_search import SCRIPT, _open

pytestmark = pytest.mark.gui

NOTE = """---
title: Aurora Studio (Reykjavík)
homepage: https://aurora.example
mood: calm
---
# Aurora Studio (Reykjavík)

Painted skies and **dusk** light, since 2009.

![The studio](<studio at dusk.png>)

See [the price list](Prices/list.txt) and [our site](https://aurora.example).

{more}
"""


@pytest.fixture
def files(tmp_path: Path) -> Path:
    return tmp_path / "asset-files"


@pytest.fixture
def session(tmp_path: Path, files: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keep = module.make_assets_demo(tmp_path)
    folder = files / "Aurora Studio"
    more = "\n\n".join(f"Paragraph {n} about the studio's work." for n in range(1, 30))
    (folder / "Aurora Studio.md").write_text(NOTE.format(more=more), encoding="utf-8")
    Image.new("RGB", (40, 20), (200, 120, 60)).save(folder / "studio at dusk.png")
    with KeepSession.open(keep, Settings()) as session:
        session.scan_all()
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


def _page(qtbot: QtBot, window: MainWindow, session: KeepSession) -> DetailPage:
    page = _open(qtbot, window, session, "Aurora Studio (Reykjavík)")
    qtbot.waitUntil(page.note_view.isVisible, timeout=5000)
    return page


def test_the_note_is_shown_under_the_title(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page(qtbot, window, session)
    note = page.note_view
    text = note.body.toPlainText()
    assert text.startswith("Painted skies and dusk light, since 2009.")  # no title heading
    assert "Paragraph 29" in text
    # Capped beside the picture; Show more expands it in place, Show less caps it again.
    assert note.more.isVisible()
    assert note.more.text() == "Show more"
    capped = note.body.height()
    assert capped == note.cap
    note.more.click()
    assert note.more.text() == "Show less"
    assert note.body.height() > capped * 3
    note.more.click()
    assert note.body.height() == capped


def test_the_notes_pictures_come_from_its_folder(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page(qtbot, window, session)
    body = page.note_view.body
    assert list(body.pictures) == ["studio at dusk.png"]
    shown = body.document().resource(
        QTextDocument.ResourceType.ImageResource.value, QUrl("studio at dusk.png")
    )
    assert isinstance(shown, QImage)
    assert shown.width() == 40
    # Nothing else is loaded, even a file that is there.
    other = body.loadResource(
        QTextDocument.ResourceType.ImageResource.value, QUrl("Fonts/Aileron-Regular.ttf")
    )
    assert other is None


def test_links_in_the_note(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    files: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _page(qtbot, window, session)
    opened: list[str] = []
    page.note_view.open_file = opened.append
    monkeypatch.setattr(field_editor, "open_web_address", opened.append)
    page.note_view.body.anchorClicked.emit(QUrl("Prices/list.txt"))
    page.note_view.body.anchorClicked.emit(QUrl("https://aurora.example"))
    assert opened == [
        os.path.normpath(files / "Aurora Studio" / "Prices" / "list.txt"),
        "https://aurora.example",
    ]


def test_pictures_and_links_from_assets_folders(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    files: Path,
) -> None:
    """Not beside the note: in the note's own assets folder (its ``assets`` key, an
    Extra field), else the keep's default (#418)."""
    folder = files / "Aurora Studio"
    note = folder / "Aurora Studio.md"
    text = note.read_text(encoding="utf-8").replace("mood: calm\n", "mood: calm\nassets: Media\n")
    text += "\n![Glow](art/glow.png) ![Logo](logo.png) [Rates](rates.txt)\n"
    note.write_text(text, encoding="utf-8")
    (folder / "Media" / "art").mkdir(parents=True)
    Image.new("RGB", (30, 10), (90, 90, 200)).save(folder / "Media" / "art" / "glow.png")
    (folder / "Media" / "rates.txt").write_text("rates", encoding="utf-8")
    (files / "Shared").mkdir()
    Image.new("RGB", (12, 12), (10, 10, 10)).save(files / "Shared" / "logo.png")
    session.scan_all()
    page = _page(qtbot, window, session)
    assert sorted(page.note_view.body.pictures) == ["art/glow.png", "studio at dusk.png"]
    opened: list[str] = []
    page.note_view.open_file = opened.append
    page.note_view.body.anchorClicked.emit(QUrl("rates.txt"))
    assert opened == [os.path.normpath(folder / "Media" / "rates.txt")]

    session.set_markdown_assets("Shared")  # the keep's default, from the top of the folder
    page.refresh()
    qtbot.waitUntil(lambda: "logo.png" in page.note_view.body.pictures, timeout=5000)
    assert page.note_view.body.pictures["logo.png"].width() == 12


def test_open_note(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    files: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acted: list[tuple[str, str]] = []

    def act(_opener: FileOpener, file: object, how: str) -> None:
        assert isinstance(file, FileToOpen), file
        acted.append((file.path, how))

    monkeypatch.setattr(FileOpener, "act", act)
    page = _page(qtbot, window, session)
    assert page.note_view.open_button.isVisibleTo(page)
    assert page.open_note_action.isVisible()
    page.note_view.open_button.click()
    qtbot.waitUntil(lambda: bool(acted), timeout=5000)
    page.open_note_action.trigger()
    qtbot.waitUntil(lambda: len(acted) == 2, timeout=5000)
    note = str(files / "Aurora Studio" / "Aurora Studio.md")
    assert acted == [(note, OPEN), (note, OPEN)]


def test_an_item_without_a_note(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _open(qtbot, window, session, "Kenji Sato")
    qtbot.waitUntil(page.note_view.isVisible, timeout=5000)  # Add a note (#383)
    assert page.note_view.add_button.isVisible()
    assert not page.note_view.body.isVisible()
    assert not page.note_view.open_button.isVisible()
    assert not page.open_note_action.isVisible()


def _extra(page: DetailPage, name: str) -> EditableValue | None:
    found = page.details.findChild(EditableValue, f"extra_{name}")
    return found if isinstance(found, EditableValue) else None


def test_the_notes_extra_fields(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _page(qtbot, window, session)
    mood = _extra(page, "mood")
    assert mood is not None
    assert not mood.marker.isHidden()
    assert mood.marker.text() == "• from the note"

    remove = next(
        b
        for b in page.details.findChildren(QToolButton)
        if b.toolTip() == "Remove 'mood'; scans won't add it back"
    )
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as removed:
        remove.click()
    assert removed.args == ["Remove 'mood' from 'Aurora Studio (Reykjavík)'."]
    qtbot.waitUntil(lambda: _extra(page, "mood") is None, timeout=5000)
    session.scan_all()  # the note still says it: left off
    page.refresh()
    qtbot.waitUntil(lambda: _extra(page, "homepage") is not None, timeout=5000)
    assert _extra(page, "mood") is None

    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.undo_action.trigger()
    qtbot.waitUntil(lambda: _extra(page, "mood") is not None, timeout=5000)
    mood = _extra(page, "mood")
    assert mood is not None
    assert mood.marker.text() == "• from the note"


# --- ignored keys (#381) ---


def _ignore_action(page: DetailPage, name: str) -> QAction:
    remove = page.details.findChild(QToolButton, f"remove_{name}")
    assert isinstance(remove, QToolButton), name
    menu = remove.menu()
    assert menu is not None
    return next(a for a in menu.actions() if a.text() == IGNORE_KEY)


def test_ignoring_a_key_in_every_note(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(qtbot, window, session)
    asked: list[str] = []
    monkeypatch.setattr(DetailPage, "confirm_ignore_key", lambda self, name: asked.append(name))
    _ignore_action(page, "mood").trigger()  # cancelled: nothing changes
    assert asked == ["mood"]
    assert session.keep.config.note_ignore_keys == []

    monkeypatch.setattr(DetailPage, "confirm_ignore_key", lambda self, name: True)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as ignored:
        _ignore_action(page, "mood").trigger()
    assert ignored.args[0].startswith("Ignore 'mood' in every note (1 item).")
    qtbot.waitUntil(lambda: _extra(page, "mood") is None, timeout=5000)
    assert load_keep_config(session.keep.toml_path).note_ignore_keys == ["mood"]
    session.scan_all()  # scans leave it out from now on
    page.refresh()
    qtbot.waitUntil(lambda: _extra(page, "homepage") is not None, timeout=5000)
    assert _extra(page, "mood") is None

    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.undo_action.trigger()
    qtbot.waitUntil(lambda: _extra(page, "mood") is not None, timeout=5000)
    assert load_keep_config(session.keep.toml_path).note_ignore_keys == []
    mood = _extra(page, "mood")
    assert mood is not None
    assert mood.marker.text() == "• from the note"


def test_the_keys_in_keep_configuration(
    qtbot: QtBot, window: MainWindow, session: KeepSession, tmp_path: Path
) -> None:
    config = window.configure_keep()
    config.settings_path = tmp_path / "settings.toml"
    config.tabs.setCurrentIndex(config.tabs.count() - 1)
    assert config.tabs.tabText(config.tabs.currentIndex()) == "Notes"

    config.keep_ignore_keys.setPlainText("Mood\n\nmood\n")
    with qtbot.waitSignal(config.changed, timeout=5000) as saved:
        QApplication.sendEvent(config.keep_ignore_keys, QFocusEvent(QEvent.Type.FocusOut))
    assert "next scan" in saved.args[0]
    assert load_keep_config(session.keep.toml_path).note_ignore_keys == ["Mood"]

    config.user_ignore_keys.setPlainText("homepage")
    with qtbot.waitSignal(config.changed, timeout=5000):
        QApplication.sendEvent(config.user_ignore_keys, QFocusEvent(QEvent.Type.FocusOut))
    assert load_settings(config.settings_path).note_ignore_keys == ["homepage"]

    config.keep_ignore_keys.setPlainText("tagalot-type")
    QApplication.sendEvent(config.keep_ignore_keys, QFocusEvent(QEvent.Type.FocusOut))
    assert "Tagalot's own" in config.notes_error.text()
    assert load_keep_config(session.keep.toml_path).note_ignore_keys == ["Mood"]

    session.scan_all()  # the lists changed: the notes are read again
    page = _page(qtbot, window, session)
    qtbot.waitUntil(
        lambda: page.details.findChild(QWidget, "section_extra") is not None, timeout=5000
    )
    assert _extra(page, "mood") is None
    assert _extra(page, "homepage") is None
    qtbot.waitUntil(lambda: config.busy == 0, timeout=5000)
