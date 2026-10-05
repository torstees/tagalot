"""A paper's authors in order on its page (#317): listed in order, Move up and Move down on
their menus, and undo."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from pytestqt.qtbot import QtBot
from sqlalchemy import Connection

from tagalot.core.ingest import IngestSession
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.search import POSITION
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.themes.loader import load_themes
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

THEME = """
from tagalot.themes.api import DetailView, Entity, Section, Theme, related


class Person(Entity):
    title_label = "Name"


class Paper(Entity):
    pass


class Papers(Theme):
    id, name, version, api_version = "papers", "Papers", 1, 4
    entities = [Person, Paper]
    relationships = [
        related("authors", Person, Paper, label="Papers", reverse_label="Authors", ordered=True)
    ]
    views = [DetailView(Paper, [Section.fields(), Section.related("authors")])]
"""


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "papers.py").write_text(THEME, encoding="utf-8")
    catalog = load_themes(user_dir=themes)
    keep_dir = tmp_path / "Papers.keep"
    create_keep(keep_dir, "Papers", ThemeRef("papers", 1))
    with KeepSession.open(keep_dir, Settings(), catalog=catalog) as session:
        yield session


def _paper(session: KeepSession, *names: str) -> int:
    """A paper by ``names``, in that order (as a file would give them)."""
    schema = session.schema
    types = {t.__name__: t for t in schema.theme.entities}

    def make(conn: Connection) -> int:
        ctx = IngestSession(conn, schema)
        paper = ctx.upsert(types["Paper"], "paper:1", title="A Paper")
        for n, name in enumerate(names):
            person = ctx.upsert(types["Person"], f"p:{name}", title=name)
            ctx.relate("authors", person, paper, position=n)
        ctx.flush()
        return paper.id

    paper_id: int = session.writer.run(make)
    return paper_id


def _authors(page: SearchPage) -> list[str]:
    return [page.model.hit(r).title for r in range(page.model.rowCount())]  # type: ignore[union-attr]


def test_move_authors_on_a_papers_page(qtbot: QtBot, session: KeepSession) -> None:
    paper = _paper(session, "Zed", "Amy", "Max")
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()
    window.open_entity(paper)
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: "authors" in page.related, timeout=5000)
    authors = page.related["authors"]
    assert authors.current_spec().sort[0].field == POSITION
    qtbot.waitUntil(lambda: _authors(authors) == ["Zed", "Amy", "Max"], timeout=5000)

    actions = dict(authors._menu_actions)
    assert {"Move up", "Move down", "Remove from authors"} <= set(actions)
    max_id = authors.model.hit(2).id  # type: ignore[union-attr]
    actions["Move up"]([max_id])  # as its right-click menu does, for the selection
    qtbot.waitUntil(lambda: _authors(authors) == ["Zed", "Max", "Amy"], timeout=5000)
    assert "Move up in A Paper's authors" in window.statusBar().currentMessage()

    window.undo_action.trigger()
    qtbot.waitUntil(lambda: _authors(authors) == ["Zed", "Amy", "Max"], timeout=5000)
