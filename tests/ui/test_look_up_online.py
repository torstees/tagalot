"""Looking up online from the window (#340): after scans, asking once per keep and only
when something would be sent, and **Look up online** on items and pages."""

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.keep import RootConfig, ThemeRef, create_keep, load_keep_config
from tagalot.core.models import Entity
from tagalot.core.online import LookupReport, Reply
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.lookups import lookup_summary
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.online import ALLOW, NEVER
from tagalot.ui.search_view import SearchPage
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

THEME = """
from tagalot.themes.api import Entity, OnlineSource, SearchView, Theme, field, role


class Book(Entity):
    roles = [role("file", kinds={"any"}, primary=True)]
    isbn: str | None = field("ISBN")
    publisher: str | None = field("Publisher", card=True)


class BooksOnline(Theme):
    id, name, version, api_version = "books_online", "Books online", 1, 5
    extensions = {".txt"}
    entities = [Book]
    views = [SearchView("Books", [Book], layout="list")]
    online_sources = [OnlineSource("Open Library", "openlibrary.org", "ISBNs", interval=0)]

    def ingest(self, batch, ctx):
        for resource in batch:
            isbn = resource.relpath.rpartition(".")[0]
            book = ctx.upsert(Book, resource.relpath, title=f"Book {isbn}", isbn=isbn)
            ctx.link(book, resource, "file")

    def online_requests(self, entity_type, item):
        return [f"https://openlibrary.org/isbn/{item.fields['isbn']}.json"]

    def online_details(self, entity, responses, ctx):
        [answer] = responses.values()
        if answer.ok:
            ctx.update(entity, publisher=answer.json()["publishers"][0])
"""


class FakeWeb:
    """Answers lookups from a table, counting requests; nothing goes online."""

    def __init__(self) -> None:
        self.answers: dict[str, Reply | Exception] = {}
        self.urls: list[str] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Reply:
        self.urls.append(url)
        answer = self.answers.get(url, Reply(404, b"{}"))
        if isinstance(answer, Exception):
            raise answer
        return answer


def _published(name: str) -> Reply:
    return Reply(200, f'{{"publishers": ["{name}"]}}'.encode())


@pytest.fixture
def web() -> FakeWeb:
    found = FakeWeb()
    found.answers["https://openlibrary.org/isbn/111.json"] = _published("Chilton")
    found.answers["https://openlibrary.org/isbn/222.json"] = _published("Ace")
    return found


@pytest.fixture
def session(tmp_path: Path, web: FakeWeb) -> Iterator[KeepSession]:
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "books_online.py").write_text(THEME, encoding="utf-8")
    files = tmp_path / "files"
    files.mkdir()
    for isbn in ("111", "222"):
        (files / f"{isbn}.txt").write_text(isbn, encoding="utf-8")
    keep = create_keep(
        tmp_path / "Books.keep",
        "Books",
        ThemeRef("books_online", 1),
        [RootConfig("files", "Files", str(files))],
    )
    with KeepSession.open(keep.dir, Settings(theme_dirs=[themes])) as opened:
        opened.online_opener = web
        yield opened


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.show()
    return window


def _scan(qtbot: QtBot, window: MainWindow) -> None:
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()


def _publishers(session: KeepSession) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    table = session.schema.by_type_id("books_online.book").table
    with session.reader.connect() as conn:
        rows = conn.execute(select(Entity.title, table.c.publisher).join(table))
        found.update({title: publisher for title, publisher in rows})
    return found


def _book(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None
    return found


def _asks(window: MainWindow, answer: str | None) -> list[str]:
    asked: list[str] = []
    assert window.lookups is not None

    def ask() -> str | None:
        asked.append("asked")
        return answer

    window.lookups.ask = ask  # type: ignore[method-assign]
    return asked


def test_the_first_scan_asks_and_allowing_looks_everything_up(
    qtbot: QtBot, window: MainWindow, session: KeepSession, web: FakeWeb
) -> None:
    asked = _asks(window, ALLOW)
    _scan(qtbot, window)
    qtbot.waitUntil(
        lambda: _publishers(session) == {"Book 111": "Chilton", "Book 222": "Ace"},
        timeout=10_000,
    )
    assert asked == ["asked"]
    assert load_keep_config(session.keep.toml_path).online_lookups == "allow"
    assert sorted(set(web.urls)) == [
        "https://openlibrary.org/isbn/111.json",
        "https://openlibrary.org/isbn/222.json",
    ]
    qtbot.waitUntil(lambda: window.lookups is not None and not window.lookups.running)

    # The next scan finds nothing new: nothing is fetched, and nothing asked.
    before = len(web.urls)
    _scan(qtbot, window)
    qtbot.wait(300)
    assert len(web.urls) == before
    assert asked == ["asked"]


def test_not_now_sends_nothing_and_isnt_asked_again(
    qtbot: QtBot, window: MainWindow, session: KeepSession, web: FakeWeb, tmp_path: Path
) -> None:
    asked = _asks(window, None)
    messages: list[str] = []
    window.statusBar().messageChanged.connect(messages.append)
    _scan(qtbot, window)
    qtbot.waitUntil(lambda: asked == ["asked"], timeout=5000)
    assert web.urls == []
    assert load_keep_config(session.keep.toml_path).online_lookups is None
    qtbot.waitUntil(lambda: "Nothing was looked up online." in messages, timeout=5000)
    (tmp_path / "files" / "333.txt").write_text("333", encoding="utf-8")
    _scan(qtbot, window)
    qtbot.wait(300)
    assert asked == ["asked"]  # not again while the keep is open
    assert web.urls == []


def test_never_then_look_up_online_offers_to_turn_them_on(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    web: FakeWeb,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _asks(window, NEVER)
    _scan(qtbot, window)
    qtbot.waitUntil(lambda: session.keep.config.online_lookups == "never", timeout=5000)
    assert web.urls == []
    assert window.lookups is not None
    offered: list[bool] = []
    monkeypatch.setattr(window.lookups, "turn_on", lambda: not offered.append(True))  # type: ignore[func-returns-value]
    window.look_up_online([_book(session, "Book 111")])
    qtbot.waitUntil(lambda: _publishers(session)["Book 111"] == "Chilton", timeout=10_000)
    assert offered == [True]
    assert session.keep.config.online_lookups == "allow"


def test_look_up_online_fetches_again(
    qtbot: QtBot, window: MainWindow, session: KeepSession, web: FakeWeb
) -> None:
    session.set_online_lookups("allow")
    _scan(qtbot, window)  # looked up after the scan, without asking
    qtbot.waitUntil(lambda: _publishers(session)["Book 111"] == "Chilton", timeout=10_000)
    assert window.lookups is not None
    qtbot.waitUntil(lambda: not window.lookups.running, timeout=5000)  # type: ignore[union-attr]
    web.answers["https://openlibrary.org/isbn/111.json"] = _published("Gollancz")
    messages: list[str] = []
    window.statusBar().messageChanged.connect(messages.append)
    window.look_up_online([_book(session, "Book 111")])
    qtbot.waitUntil(lambda: _publishers(session)["Book 111"] == "Gollancz", timeout=10_000)
    qtbot.waitUntil(lambda: "Looked up 1 item online." in messages, timeout=5000)


def test_an_unreachable_service_is_reported(
    qtbot: QtBot, window: MainWindow, session: KeepSession, web: FakeWeb
) -> None:
    session.set_online_lookups("allow")
    web.answers["https://openlibrary.org/isbn/111.json"] = OSError("offline")
    web.answers["https://openlibrary.org/isbn/222.json"] = OSError("offline")
    messages: list[str] = []
    window.statusBar().messageChanged.connect(messages.append)
    _scan(qtbot, window)
    qtbot.waitUntil(lambda: any("left for later" in m for m in messages), timeout=10_000)
    assert "Open Library: can't be reached (offline)" in [
        p.message for p in session.problems.items()
    ]
    qtbot.waitUntil(lambda: window.problem_badge.isVisible(), timeout=5000)


def test_the_menus_offer_look_up_online(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    _asks(window, None)
    _scan(qtbot, window)
    window.navigation.select(NavTarget("view", "Books", "Books"))
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    qtbot.waitUntil(lambda: page.model.hit(0) is not None, timeout=5000)
    hit = page.model.hit(0)
    assert hit is not None
    menu = page.item_menu(hit)
    assert "Look up online" in [a.text() for a in menu.actions()]
    window.open_entity(hit.id)
    detail = window.stack.currentWidget()
    assert isinstance(detail, DetailPage)
    qtbot.waitUntil(lambda: detail._look_up_added, timeout=5000)
    requested: list[list[int]] = []
    detail.look_up_requested.connect(requested.append)
    [action] = [a for a in detail._more_menu.actions() if a.objectName() == "look_up"]
    action.trigger()
    assert requested == [[hit.id]]


def test_summaries() -> None:
    assert lookup_summary(LookupReport(), by_user=True) == (
        "Nothing to look up online for these items."
    )
    assert lookup_summary(LookupReport(), by_user=False) == ""
    assert lookup_summary(LookupReport(items=2, not_found=1), by_user=False) == (
        "Looked up 2 items online, 1 not found."
    )
    assert lookup_summary(LookupReport(skipped=1), by_user=True) == (
        "Looked up 0 items online, 1 left for later (a service couldn't be reached)."
    )
