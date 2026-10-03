"""The movies theme's pages in the window: a movie's screenshot gallery (#124), and its
cast by hand (#260)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QListWidget, QPushButton
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.relate_dialog import RelateDialog
from tagalot.ui.workers import ScanController
from tests.ui.test_within import _search

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


def _page_of(qtbot: QtBot, window: MainWindow, session: KeepSession, title: str) -> DetailPage:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None
    window.open_entity(found)
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: page.detail is not None, timeout=5000)
    return page


def _cast_of(page: DetailPage) -> list[str]:
    assert page.detail is not None
    [section] = [s for s in page.detail.sections if s.kind == "related"]
    assert section.title == "Cast"
    return [e.title for e in section.entities]


def test_cast_by_hand(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _page_of(qtbot, window, session, "Heat")
    assert _cast_of(page) == ["Al Pacino", "Robert De Niro"]
    asked: list[tuple[str, str, str, set[int]]] = []

    def choose(
        title: str, other_type: str, section: str, already: set[int]
    ) -> tuple[int | None, str | None]:
        asked.append((title, other_type, section, already))
        return None, "Val Kilmer"

    window.choose_related = choose  # type: ignore[method-assign]
    add = page.findChild(QPushButton, "add_cast")
    assert add is not None
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        add.click()
    assert blocker.args == ["Add 'Val Kilmer' to Heat's cast."]
    assert asked[0][:3] == ("Heat", "movies.actor", "Cast")
    assert len(asked[0][3]) == 2  # Pacino and De Niro are already there
    qtbot.waitUntil(lambda: "Val Kilmer" in _cast_of(page), timeout=5000)

    # The cast is a search of its own: list and grid, and Remove on an item's menu.
    cast = page.related["cast"]
    assert cast.layout_mode == "grid"
    qtbot.waitUntil(lambda: cast.status.text() == "3 items", timeout=5000)
    de_niro = next(
        hit for row in range(3) if (hit := cast.model.hit(row)) and hit.title == "Robert De Niro"
    )
    [remove] = [a for a in cast.item_menu(de_niro).actions() if a.text() == "Remove from cast"]
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        remove.trigger()
    assert blocker.args == [
        "Remove 'Robert De Niro' from Heat's cast. Edit \u2192 Undo brings it back."
    ]
    qtbot.waitUntil(lambda: _cast_of(page) == ["Al Pacino", "Val Kilmer"], timeout=5000)
    qtbot.waitUntil(lambda: cast.status.text() == "2 items", timeout=5000)

    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.tag_actions.undo()
    qtbot.waitUntil(lambda: "Robert De Niro" in _cast_of(page), timeout=5000)


def _id(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None
    return found


def test_the_add_to_cast_dialog(qtbot: QtBot, session: KeepSession) -> None:
    pacino = _id(session, "Al Pacino")
    dialog = RelateDialog(session, "Heat", "movies.actor", "Cast", {pacino})
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.results.count() == 6, timeout=5000)  # every actor
    dialog.search.setText("al")
    dialog.look_up()
    qtbot.waitUntil(lambda: dialog.results.count() == 2, timeout=5000)
    texts = [dialog.results.item(n).text() for n in range(dialog.results.count())]
    assert texts == ["New actor: al", "Al Pacino"]
    assert not dialog.results.item(1).flags() & Qt.ItemFlag.ItemIsEnabled  # already there
    assert dialog.chosen() == (None, "al")
    dialog.search.setText("Al Pacino")
    dialog.look_up()
    qtbot.waitUntil(lambda: dialog.results.count() == 1, timeout=5000)  # no "New": he exists
    assert not dialog.add_button.isEnabled()


@pytest.mark.parametrize(
    ("view", "count", "first"),
    [
        ("Movies", "9 items", "Alien"),
        ("Actors", "6 items", "Al Pacino"),
        ("Collections", "3 items", "Alien Collection"),
    ],
)
def test_the_views(qtbot: QtBot, window: MainWindow, view: str, count: str, first: str) -> None:
    page = _search(qtbot, window, NavTarget("view", key=view, label=view), count)
    assert page.layout_mode == "grid"
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    hit = page.model.hit(0)
    assert hit is not None
    assert hit.title == first  # by title


def test_tags_on_a_collection_count_for_its_movies(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _search(qtbot, window, NavTarget("view", key="Movies", label="Movies"), "9 items")
    tree = session.tag_cache.get()
    fantasy = next(t for t in tree if tree.node(t).name == "Fantasy")  # on the collection
    page.filter_bar.add_tag(fantasy)
    qtbot.waitUntil(lambda: page.status.text() == "3 items", timeout=5000)


def test_an_actor_page_lists_their_filmography(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page_of(qtbot, window, session, "Leonardo DiCaprio")
    assert page.detail is not None
    [section] = [s for s in page.detail.sections if s.kind == "related"]
    assert section.title == "Filmography"
    assert [e.title for e in section.entities] == ["Inception"]
    assert page.findChild(QPushButton, "add_cast") is not None
    films = page.related["cast"]
    qtbot.waitUntil(lambda: films.status.text() == "1 item", timeout=5000)
    qtbot.waitUntil(lambda: films.model.hit(0) is not None, timeout=5000)
    hit = films.model.hit(0)
    assert hit is not None
    assert hit.title == "Inception"
    films.list_button.click()  # the list works too
    assert films.layout_mode == "list"


def test_a_collection_lists_its_movies_by_year(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _page_of(qtbot, window, session, "The Lord of the Rings")
    contents = page.contents
    assert contents is not None
    qtbot.waitUntil(lambda: contents.status.text() == "3 items", timeout=5000)
    qtbot.waitUntil(lambda: contents.model.hit(2) is not None, timeout=5000)
    hits = [contents.model.hit(r) for r in range(3)]
    assert [h.title for h in hits if h] == [
        "The Fellowship of the Ring",
        "The Two Towers",
        "The Return of the King",
    ]
