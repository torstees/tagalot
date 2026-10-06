"""Reading documents' text after a scan, from the window (#348)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from pytestqt.qtbot import QtBot

from tagalot.core.contents import ContentsResult
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.main_window import MainWindow
from tagalot.ui.workers import ScanController
from tests.core.book_files import paged_pdf
from tests.core.test_contents import THEME

pytestmark = pytest.mark.gui


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    themes, files = tmp_path / "themes", tmp_path / "files"
    themes.mkdir()
    files.mkdir()
    (themes / "docs.py").write_text(THEME, encoding="utf-8")
    (files / "paper.pdf").write_bytes(paged_pdf([["Attention"], ["is all you need"]]))
    keep = create_keep(
        tmp_path / "Docs.keep", "Docs", ThemeRef("docs", 1), [RootConfig("f", "F", str(files))]
    )
    with KeepSession.open(keep.dir, Settings(theme_dirs=[themes])) as opened:
        yield opened


def test_a_scan_reads_new_documents_text(qtbot: QtBot, session: KeepSession) -> None:
    session.set_contents_index("words")
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    with qtbot.waitSignal(window._contents_done, timeout=10_000) as blocker:
        window.scan_now()
    result = blocker.args[0]
    assert isinstance(result, ContentsResult)
    assert (result.read, result.pages) == (1, 2)
    assert not window.contents_status.isVisible()


def test_nothing_is_read_while_off(qtbot: QtBot, session: KeepSession) -> None:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    qtbot.wait(300)
    assert not (session.keep.dir / "fulltext.db").exists()
