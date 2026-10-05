"""Research actions in the window (#322), on the research demo: the clipboard, the browser,
saving an export (outside watched folders, inside one after a warning and then skipped, never
over an existing file), and Cites / Cited by on a paper's page."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.keep import load_keep_config
from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
ATTENTION = "Attention Is All You Need"
RESNET = "Deep Residual Learning for Image Recognition"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_research_demo(tmp_path), Settings()) as session:
        yield session


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()
    qtbot.waitUntil(lambda: window.tag_panel.loaded, timeout=5000)
    return window


def _paper(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(
            select(Entity.id).where(Entity.title == title, Entity.type == "research.paper")
        )
    assert found is not None
    return found


def _messages(window: MainWindow) -> list[str]:
    messages: list[str] = []
    window.statusBar().messageChanged.connect(messages.append)
    return messages


def test_copy_citation_and_open_doi_page(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("tagalot.ui.main_window.open_web_address", opened.append)
    messages = _messages(window)
    window.run_action("copy_citation", [_paper(session, ATTENTION)])
    qtbot.waitUntil(lambda: "Copied 1 citation." in messages, timeout=5000)
    authors = "Vaswani, A., Shazeer, N., Parmar, N., & Uszkoreit, J."
    assert QApplication.clipboard().text().startswith(f"{authors} (2017). {ATTENTION}.")
    window.run_action("open_doi_page", [_paper(session, RESNET)])
    qtbot.waitUntil(lambda: bool(opened), timeout=5000)
    assert opened == ["https://doi.org/10.1109/cvpr.2016.90"]


def test_export_outside_and_inside_a_watched_folder(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attention = _paper(session, ATTENTION)
    asked: list[str] = []

    def confirm(root: str, relpath: str) -> bool:
        asked.append(relpath)
        return True

    monkeypatch.setattr(window, "confirm_export_in_root", confirm)
    messages = _messages(window)

    outside = tmp_path / "out" / "attention.bib"
    outside.parent.mkdir()
    monkeypatch.setattr(window, "choose_export_path", lambda name: str(outside))
    window.run_action("export_bibtex", [attention])
    qtbot.waitUntil(lambda: outside.exists(), timeout=5000)
    assert "@inproceedings{vaswani_attention_2017," in outside.read_text(encoding="utf-8")
    assert not asked  # not a watched folder: no warning

    files = Path(session.root_path("papers"))
    inside = files / "Exports" / "my papers.bib"
    inside.parent.mkdir()
    monkeypatch.setattr(window, "choose_export_path", lambda name: str(inside))
    window.run_action("export_bibtex", [attention])
    qtbot.waitUntil(lambda: inside.exists(), timeout=5000)
    assert asked == ["Exports/my papers.bib"]
    root = load_keep_config(session.keep.toml_path).roots[0]
    assert "Exports/my papers.bib" in root.exclude  # skipped: never read back as a source
    qtbot.waitUntil(lambda: any("scans skip it" in m for m in messages), timeout=5000)

    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warned.append(a[2]))
    before = inside.read_text(encoding="utf-8")
    window.run_action("export_bibtex", [_paper(session, RESNET)])
    qtbot.waitUntil(lambda: bool(warned), timeout=5000)
    assert "never replaces files there" in warned[0]
    assert inside.read_text(encoding="utf-8") == before


def test_cites_and_cited_by_on_a_paper_page(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    attention, resnet = _paper(session, ATTENTION), _paper(session, RESNET)
    window.open_entity(attention)
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: {"cites", "cites:b", "related"} <= set(page.related), timeout=5000)
    asked: list[tuple[str, set[int]]] = []

    def choose(
        title: str, other_type: str, section: str, already: set[int]
    ) -> tuple[int | None, str | None]:
        asked.append((section, already))
        return resnet, None

    window.choose_related = choose  # type: ignore[method-assign]
    [add] = page.related["cites:b"].findChildren(QPushButton, "add_cites")  # Cited by's
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        add.click()  # Cited by: ResNet cites Attention
    assert blocker.args == [f"Add '{RESNET}' to {ATTENTION}'s cited by."]
    assert asked[0] == ("Cited by", {attention})  # never itself
    cited_by = page.related["cites:b"]
    qtbot.waitUntil(lambda: cited_by.status.text() == "1 item", timeout=5000)
    cites = page.related["cites"]
    qtbot.waitUntil(lambda: cites.status.text() == "Nothing found", timeout=5000)
