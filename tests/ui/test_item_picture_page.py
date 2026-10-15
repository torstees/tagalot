"""Setting an item's own picture from its page (#439), on the books demo: Set picture…
for an author, the page saying where its picture comes from, Use the usual picture, and
Edit → Undo; and Use as its picture on a file of an item."""

from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.detail import FileRow
from tagalot.core.item_pictures import USER, own_picture
from tagalot.core.models import Resource, ResourceStatus
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tests.ui.test_contents_search import _open
from tests.ui.test_file_extra_page import window
from tests.ui.test_within import _search

pytestmark = pytest.mark.gui

__all__ = ["window"]  # fixtures


def test_set_picture_and_go_back(
    qtbot: QtBot, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = window.session
    assert session is not None
    cover = Path(session.root_path("books")) / "Royal Road/images/mol-cover.jpg"
    monkeypatch.setattr(window, "choose_picture_file", lambda: str(cover))
    forgotten: list[list[int]] = []
    window.tag_actions.pictures_changed.connect(forgotten.append)

    page = _open(qtbot, window, session, "nobody103")
    author = page.entity_id
    assert page.thumbnail.toolTip() == ""
    assert not page.usual_picture_action.isVisible()
    page.set_picture_action.trigger()  # as choosing More → Set picture… does
    qtbot.waitUntil(
        lambda: page.detail is not None and page.detail.picture is not None, timeout=5000
    )
    assert page.detail is not None
    assert page.detail.picture is not None
    assert (page.detail.picture.relpath, page.detail.picture.source) == (
        "Royal Road/images/mol-cover.jpg",
        USER,
    )
    assert page.thumbnail.toolTip() == "Its picture, chosen by you: mol-cover.jpg"
    assert page.usual_picture_action.isVisible()
    assert window.statusBar().currentMessage().startswith("Set nobody103's picture.")
    assert forgotten == [[author]]  # its thumbnail is loaded again

    page.usual_picture_action.trigger()
    qtbot.waitUntil(lambda: page.detail is not None and page.detail.picture is None, timeout=5000)
    assert page.thumbnail.toolTip() == ""
    window.tag_actions.undo()  # the chosen picture is back
    qtbot.waitUntil(
        lambda: page.detail is not None and page.detail.picture is not None, timeout=5000
    )
    assert forgotten[-1] == [author]


def test_a_picture_outside_the_watched_folders_is_refused(
    qtbot: QtBot, window: MainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = window.session
    assert session is not None
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(b"png")
    monkeypatch.setattr(window, "choose_picture_file", lambda: str(outside))
    page = _open(qtbot, window, session, "nobody103")
    page.set_picture_action.trigger()
    refused = "choose a picture in a watched folder"
    qtbot.waitUntil(lambda: refused in window.statusBar().currentMessage(), timeout=5000)
    assert page.detail is not None
    assert page.detail.picture is None


def test_use_a_file_of_an_item_as_its_picture(qtbot: QtBot, window: MainWindow) -> None:
    session = window.session
    assert session is not None
    page = _open(qtbot, window, session, "Mother of Learning")
    with session.reader.connect() as conn:
        rid = conn.scalar(select(Resource.id).where(Resource.relpath.like("%mol-cover.jpg")))
    assert rid is not None
    relpath = "Royal Road/images/mol-cover.jpg"
    cover = FileRow(rid, "Book files", relpath, "file", 1, ResourceStatus.OK, None, role="cover")
    actions = {a.text(): a for a in page._file_menu(cover).actions()}
    actions["Use as its picture"].trigger()
    qtbot.waitUntil(
        lambda: page.detail is not None and page.detail.picture is not None, timeout=5000
    )
    assert page.thumbnail.toolTip() == "Its picture, chosen by you: mol-cover.jpg"


def test_set_picture_from_a_search(
    qtbot: QtBot, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = window.session
    assert session is not None
    cover = Path(session.root_path("books")) / "Royal Road/images/mol-cover.jpg"
    monkeypatch.setattr(window, "choose_picture_file", lambda: str(cover))
    page = _search(qtbot, window, NavTarget("view", key="Authors", label="Authors"), "15 items")
    hit = next(
        h
        for r in range(page.model.rowCount())
        if (h := page.model.hit(r)) and h.title == "pirateaba"
    )
    actions = {a.text(): a for a in page.item_menu(hit).actions()}
    actions["Set picture…"].trigger()

    def chosen() -> bool:
        with session.reader.connect() as conn:  # a new read each time: it sees the change
            return own_picture(conn, hit.id) is not None

    qtbot.waitUntil(chosen, timeout=5000)
