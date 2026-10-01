"""Link to item… from Triage, Unlink from an item's page, and the dialog (#243)."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QWidget
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.detail import FileRow
from tagalot.core.models import EntityResource, Resource
from tagalot.core.session import KeepSession
from tagalot.themes.api import Kind
from tagalot.ui.link_dialog import LinkDialog
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.triage import TriagePage
from tests.ui.test_contents_search import _id, _open, session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures: the assets demo


def _triage(qtbot: QtBot, window: MainWindow) -> TriagePage:
    window.navigation.select(NavTarget("triage", label="Triage"))
    page = window.stack.currentWidget()
    assert isinstance(page, TriagePage)
    page.refresh()
    qtbot.waitUntil(lambda: page.counts is not None, timeout=5000)
    return page


def _files(detail: QWidget) -> list[str]:
    box = detail.findChild(QWidget, "section_role")
    assert box is not None
    return [label.text() for label in box.findChildren(QLabel)]


def test_the_dialog_lists_items_whose_roles_take_the_file(
    qtbot: QtBot, session: KeepSession
) -> None:
    dialog = LinkDialog(session, "gem.png", [Kind.IMAGE])
    qtbot.addWidget(dialog)
    assert list(dialog.types) == ["assets2d.image"]  # artists hold folders, fonts fonts…
    qtbot.waitUntil(lambda: dialog.results.count() > 0, timeout=5000)
    dialog.search.setText("heart")
    dialog.look_up()
    qtbot.waitUntil(lambda: dialog.results.count() == 1, timeout=5000)
    assert dialog.results.item(0).text().startswith("heart.png")
    assert [dialog.role.itemText(i) for i in range(dialog.role.count())] == ["file"]
    assert dialog.chosen() == (_id(session, "heart.png"), "file")
    assert dialog.link_button.isEnabled()

    nothing = LinkDialog(session, "song.mp3", [Kind.AUDIO])
    qtbot.addWidget(nothing)
    assert "No kind of item" in nothing.note.text()
    assert not nothing.link_button.isEnabled()


def test_linking_an_unlinked_file_and_unlinking_it(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    gem, heart = _id(session, "gem.png"), _id(session, "heart.png")
    session.delete_items([gem])  # its file is now no item's
    page = _triage(qtbot, window)
    gem_file = next(i for i, f in enumerate(page.files.files) if f.relpath.endswith("gem.png"))
    page.table.selectRow(gem_file)
    window.choose_link_target = lambda what, kinds: (heart, "file")  # type: ignore[method-assign]
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        page._link()
    assert blocker.args == ["Link 1 file to heart.png."]
    qtbot.waitUntil(
        lambda: not any(f.relpath.endswith("gem.png") for f in page.files.files), timeout=5000
    )
    # heart's file role holds one file: yours replaced its own, which is now unlinked.
    assert any(f.relpath.endswith("heart.png") for f in page.files.files)

    detail = _open(qtbot, window, session, "heart.png")
    rows = _files(detail)
    assert any("gem.png" in t and "linked by you" in t for t in rows)
    assert detail.detail is not None
    mine = next(f for s in detail.detail.sections for f in s.files if f.by_user)
    assert isinstance(mine, FileRow)
    menu = detail._file_menu(mine)
    unlink = next(a for a in menu.actions() if a.text() == "Unlink from this item")
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        unlink.trigger()
    assert blocker.args == ["Unlink a file from heart.png."]

    window.undo_action.trigger()  # the unlink
    qtbot.waitUntil(
        lambda: window.undo_action.text() == "Undo Link 1 file to heart.png", timeout=5000
    )
    window.undo_action.trigger()  # the link
    qtbot.waitUntil(
        lambda: window.redo_action.text() == "Redo Link 1 file to heart.png", timeout=5000
    )
    with session.reader.connect() as conn:
        files = conn.scalars(
            select(Resource.relpath)
            .join(EntityResource, EntityResource.resource_id == Resource.id)
            .where(EntityResource.entity_id == heart)
        ).all()
    assert [Path(f).name for f in files] == ["heart.png"]  # back as it was
