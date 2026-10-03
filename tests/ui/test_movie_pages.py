"""The movies theme's pages in the window (#124): a movie's screenshot gallery."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QListWidget
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
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
    with KeepSession.open(module.make_movies_demo(tmp_path), Settings()) as session:
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


def test_a_screenshot_opens_from_the_gallery(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    nothing_is_launched: list[tuple[str, str]],
) -> None:
    with session.reader.connect() as conn:
        inception = conn.scalar(select(Entity.id).where(Entity.title == "Inception"))
    assert inception is not None
    window.open_entity(inception)
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: page.findChild(QListWidget, "gallery_Screenshot") is not None)
    gallery = page.findChild(QListWidget, "gallery_Screenshot")
    assert gallery is not None
    assert [gallery.item(n).text() for n in range(gallery.count())] == [
        "still 1.jpg",
        "still 2.jpg",
    ]
    gallery.itemActivated.emit(gallery.item(1))  # a double-click, or Enter
    qtbot.waitUntil(lambda: bool(nothing_is_launched), timeout=5000)
    [(how, path)] = nothing_is_launched
    assert how == "open"
    assert (
        Path(path) == Path(session.root_path("movies")) / "Inception (2010)/screenshots/still 2.jpg"
    )
