"""Projects in the window (#329), on the research demo: New project… in the Projects view,
Add to project… on papers (an existing project, or a new one by name), and Remove from the
project on its page."""

import pytest
from PySide6.QtWidgets import QPushButton
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import Entity, EntityContains
from tagalot.core.session import KeepSession
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.search_view import SearchPage
from tests.ui.test_research_actions import ATTENTION, RESNET, _paper, session, window

pytestmark = pytest.mark.gui
__all__ = ["session", "window"]  # fixtures


def _contents(session: KeepSession, title: str) -> list[str]:
    with session.reader.connect() as conn:
        project = conn.scalar(
            select(Entity.id).where(Entity.title == title, Entity.type == "research.project")
        )
        if project is None:
            return []
        return sorted(
            conn.scalars(
                select(Entity.title)
                .join(EntityContains, EntityContains.child_id == Entity.id)
                .where(EntityContains.parent_id == project)
            )
        )


def test_new_project_from_the_projects_view(
    qtbot: QtBot, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(window, "ask_new_name", lambda noun: "Thesis reading")
    window.navigation.select(NavTarget("view", key="Projects", label="Projects"))
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    new = page.findChild(QPushButton, "new_research.project")
    assert new is not None
    assert new.text() == "New project…"
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        new.click()
    assert blocker.args == ["New project 'Thesis reading'."]
    qtbot.waitUntil(lambda: isinstance(window.stack.currentWidget(), DetailPage), timeout=5000)
    detail = window.stack.currentWidget()
    assert isinstance(detail, DetailPage)
    qtbot.waitUntil(lambda: detail.detail is not None, timeout=5000)
    assert detail.detail is not None
    assert detail.detail.title == "Thesis reading"


def test_add_to_a_new_project_and_remove_from_it(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    attention, resnet = _paper(session, ATTENTION), _paper(session, RESNET)
    window.choose_container = lambda type_id, count: (None, "Thesis")  # type: ignore[method-assign]
    window.add_to_container("research.project", [attention, resnet])
    qtbot.waitUntil(lambda: len(_contents(session, "Thesis")) == 2, timeout=5000)

    with session.reader.connect() as conn:
        thesis = conn.scalar(select(Entity.id).where(Entity.title == "Thesis"))
    assert thesis is not None
    window.open_entity(thesis)
    page = window.stack.currentWidget()
    assert isinstance(page, DetailPage)
    qtbot.waitUntil(lambda: page.contents is not None, timeout=5000)
    assert page.contents is not None
    actions = dict(page.contents._menu_actions)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        actions["Remove from Thesis"]([resnet])  # as its right-click menu does
    assert blocker.args == [f"Remove '{RESNET}' from Thesis. Edit → Undo puts them back."]
    assert _contents(session, "Thesis") == [ATTENTION]
    window.undo_action.trigger()
    qtbot.waitUntil(lambda: len(_contents(session, "Thesis")) == 2, timeout=5000)


def test_papers_offer_add_to_project(qtbot: QtBot, window: MainWindow) -> None:
    window.navigation.select(NavTarget("view", key="Papers", label="Papers"))
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    qtbot.waitUntil(lambda: page.model.rowCount() > 0, timeout=5000)
    hit = page.model.hit(0)
    assert hit is not None
    labels = [a.text() for a in page.item_menu(hit).actions()]
    assert "Add to project…" in labels
