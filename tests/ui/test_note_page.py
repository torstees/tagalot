"""An item's note on its page (#380, DESIGN.md §9 *Notes*, §12): the body rendered under
the title, capped with Show more, its pictures and links from the note's folder; Open
note; and Extra fields from the note, marked, editable, and removable for good."""

import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QThreadPool, QUrl
from PySide6.QtGui import QImage, QTextDocument
from PySide6.QtWidgets import QToolButton
from pytestqt.qtbot import QtBot

from tagalot.core.handlers import OPEN, FileToOpen
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui import field_editor
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.field_editor import EditableValue
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.main_window import MainWindow
from tagalot.ui.workers import ScanController
from tests.ui.test_contents_search import SCRIPT, _open

pytestmark = pytest.mark.gui

NOTE = """---
title: Aurora Studio (Reykjavík)
website: https://aurora.example
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
    assert not page.note_view.isVisible()
    assert not page.open_note_action.isVisible()


def _extra(page: DetailPage, name: str) -> EditableValue | None:
    found = page.findChild(EditableValue, f"extra_{name}")
    return found if isinstance(found, EditableValue) else None


def test_the_notes_extra_fields(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _page(qtbot, window, session)
    mood = _extra(page, "mood")
    assert mood is not None
    assert not mood.marker.isHidden()
    assert mood.marker.text() == "• from the note"

    remove = next(
        b
        for b in page.findChildren(QToolButton)
        if b.toolTip() == "Remove 'mood'; scans won't add it back from the note"
    )
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as removed:
        remove.click()
    assert removed.args == ["Remove 'mood' from 'Aurora Studio (Reykjavík)'."]
    qtbot.waitUntil(lambda: _extra(page, "mood") is None, timeout=5000)
    session.scan_all()  # the note still says it: left off
    page.refresh()
    qtbot.waitUntil(lambda: _extra(page, "website") is not None, timeout=5000)
    assert _extra(page, "mood") is None

    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.undo_action.trigger()
    qtbot.waitUntil(lambda: _extra(page, "mood") is not None, timeout=5000)
    mood = _extra(page, "mood")
    assert mood is not None
    assert mood.marker.text() == "• from the note"
