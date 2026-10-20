"""Extra fields from an item's own file on its page (#410): marked as from the file, with
the menu to ignore the key everywhere."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QToolButton
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.detail_view import FROM_FILE_TIP, IGNORE_KEY
from tagalot.ui.field_editor import EditableValue
from tagalot.ui.main_window import MainWindow
from tagalot.ui.workers import ScanController
from tests.ui.test_contents_search import SCRIPT, _open

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_books_demo(tmp_path), Settings()) as session:
        window = MainWindow(session, scans=ScanController(QThreadPool()))
        qtbot.addWidget(window)
        window.resize(1200, 800)
        window.show()
        yield window
        window.thumbnails.clear()
        window.thumbnails.wait()


def test_a_markdown_books_own_keys_are_marked_from_the_file(
    qtbot: QtBot, window: MainWindow
) -> None:
    assert window.session is not None
    page = _open(qtbot, window, window.session, "The Lighthouse Keeper")
    status = page.details.findChild(EditableValue, "extra_status")
    assert isinstance(status, EditableValue)
    assert status.marker.text() == "• from the file"
    assert status.marker.toolTip() == FROM_FILE_TIP
    assert page.details.findChild(EditableValue, "extra_cssclasses") is None  # ignored
    remove = page.details.findChild(QToolButton, "remove_status")
    assert isinstance(remove, QToolButton)
    menu = remove.menu()
    assert menu is not None
    assert IGNORE_KEY in [a.text() for a in menu.actions()]
