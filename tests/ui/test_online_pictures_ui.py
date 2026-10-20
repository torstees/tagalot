"""Pictures from online services in the window (#483): a keep that allowed lookups before
is asked once about a service added since, and a fetched picture shows on the item."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QMessageBox, QWidget
from pytestqt.qtbot import QtBot

from tagalot.core.keep import RootConfig, ThemeRef, create_keep, load_keep_config
from tagalot.core.online import Reply
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.themes.api import OnlineSource
from tagalot.ui.main_window import MainWindow
from tagalot.ui.online import ALLOW, REFUSE, ask_new_online_services
from tagalot.ui.workers import ScanController
from tests.core.book_files import jpeg
from tests.ui.test_look_up_online import FakeWeb, _book, _published, _scan

pytestmark = pytest.mark.gui

COVERS = OnlineSource("Open Library Covers", "covers.openlibrary.org", "ISBNs")

PICTURES_THEME = """
from tagalot.themes.api import Entity, OnlineSource, SearchView, Theme, field, role


class Book(Entity):
    roles = [role("file", kinds={"any"}, primary=True)]
    isbn: str | None = field("ISBN")
    publisher: str | None = field("Publisher", card=True)


class BooksOnline(Theme):
    id, name, version, api_version = "books_online", "Books online", 1, 21
    extensions = {".txt"}
    entities = [Book]
    views = [SearchView("Books", [Book], layout="list")]
    online_sources = [
        OnlineSource("Open Library", "openlibrary.org", "ISBNs", interval=0),
        OnlineSource("Open Library Covers", "covers.openlibrary.org", "ISBNs", interval=0),
    ]

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
            isbn = ctx.get(entity).fields["isbn"]
            ctx.online_picture(entity, f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg")
"""
"""The lookup tests' theme (``test_look_up_online``), also naming each book's cover on a
second service."""


def _cover(isbn: str) -> str:
    return f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"


@pytest.fixture
def web() -> FakeWeb:
    found = FakeWeb()
    found.answers["https://openlibrary.org/isbn/111.json"] = _published("Chilton")
    found.answers[_cover("111")] = Reply(200, jpeg())
    return found


@pytest.fixture
def session(tmp_path: Path, web: FakeWeb) -> Iterator[KeepSession]:
    assert "covers.openlibrary.org" in PICTURES_THEME
    assert "ctx.online_picture" in PICTURES_THEME
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "books_online.py").write_text(PICTURES_THEME, encoding="utf-8")
    files = tmp_path / "files"
    files.mkdir()
    (files / "111.txt").write_text("111", encoding="utf-8")
    keep = create_keep(
        tmp_path / "Books.keep",
        "Books",
        ThemeRef("books_online", 1),
        [RootConfig("files", "Files", str(files))],
    )
    # Lookups allowed before services were asked about one by one (Open Library only).
    keep.toml_path.write_text(
        keep.toml_path.read_text(encoding="utf-8") + "\n[online]\nlookups = 'allow'\n",
        encoding="utf-8",
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


def _asks_new(window: MainWindow, answer: str | None) -> list[list[str]]:
    asked: list[list[str]] = []
    assert window.lookups is not None

    def ask_new(sources: list[OnlineSource]) -> str | None:
        asked.append([s.name for s in sources])
        return answer

    window.lookups.ask_new = ask_new  # type: ignore[method-assign]
    return asked


def test_a_new_service_is_asked_about_and_its_picture_shows(
    qtbot: QtBot, window: MainWindow, session: KeepSession, web: FakeWeb
) -> None:
    asked = _asks_new(window, ALLOW)
    _scan(qtbot, window)
    qtbot.waitUntil(lambda: _cover("111") in web.urls, timeout=10_000)
    assert asked == [["Open Library Covers"]]
    config = load_keep_config(session.keep.toml_path)
    assert config.online_allowed == ["covers.openlibrary.org", "openlibrary.org"]
    book = _book(session, "Book 111")

    def shows_a_picture() -> bool:
        loaded = window.thumbnails.get(book)
        return loaded is not None and loaded.image is not None

    qtbot.waitUntil(shows_a_picture, timeout=10_000)  # no file: the picture from the service


def test_not_now_looks_up_the_rest_and_asks_again_next_time(
    qtbot: QtBot, window: MainWindow, session: KeepSession, web: FakeWeb
) -> None:
    asked = _asks_new(window, None)
    _scan(qtbot, window)
    qtbot.waitUntil(lambda: "https://openlibrary.org/isbn/111.json" in web.urls, timeout=10_000)
    assert window.lookups is not None
    qtbot.waitUntil(lambda: not window.lookups.running, timeout=10_000)  # type: ignore[union-attr]
    assert _cover("111") not in web.urls  # nothing sent to the new service
    assert asked == [["Open Library Covers"]]
    assert load_keep_config(session.keep.toml_path).online_allowed is None
    assert window.lookups.not_now_new


def test_dont_use_it_is_remembered(
    qtbot: QtBot, window: MainWindow, session: KeepSession, web: FakeWeb
) -> None:
    _asks_new(window, REFUSE)
    _scan(qtbot, window)
    # The session takes the new settings after writing keep.toml: wait for it, not the file.
    qtbot.waitUntil(lambda: session.new_online_sources() == [], timeout=10_000)
    assert session.keep.config.online_refused == ["covers.openlibrary.org"]
    assert load_keep_config(session.keep.toml_path).online_refused == ["covers.openlibrary.org"]
    assert window.lookups is not None
    qtbot.waitUntil(lambda: not window.lookups.running, timeout=10_000)  # type: ignore[union-attr]
    assert _cover("111") not in web.urls


@pytest.mark.parametrize(
    ("button", "answer"), [("Allow", ALLOW), ("Not now", None), ("Don't use it", REFUSE)]
)
def test_the_question_names_the_new_service(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, button: str, answer: str | None
) -> None:
    shown: list[str] = []

    def answer_it(box: QMessageBox) -> int:
        shown.append(box.informativeText())
        assert [b.text() for b in box.buttons()] == ["Allow", "Not now", "Don't use it"]
        assert box.defaultButton().text() == "Not now"
        next(b for b in box.buttons() if b.text() == button).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", answer_it)
    parent = QWidget()
    qtbot.addWidget(parent)
    assert ask_new_online_services(parent, [COVERS]) == answer
    assert shown[0].startswith(
        "This keep looks details up online, and Tagalot can now also use Open Library "
        "Covers. Tagalot would send ISBNs to Open Library Covers."
    )
