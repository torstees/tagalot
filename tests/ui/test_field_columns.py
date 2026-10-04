"""Every field as a list column (#314): card fields shown, the others hidden until the user
shows them from the header menu, and remembered per view. On the books demo, whose Link,
Description, and ISBN aren't card fields."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.ui_state import load_ui_state
from tagalot.ui.main_window import MainWindow
from tagalot.ui.models.results import ResultColumn
from tagalot.ui.navigation import NavTarget
from tagalot.ui.result_table import hidden_keys, list_columns
from tagalot.ui.search_view import SearchPage
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
    with KeepSession.open(module.make_books_demo(tmp_path), Settings()) as session:
        yield session


def _books(qtbot: QtBot, session: KeepSession) -> SearchPage:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()
    window.navigation.select(NavTarget("view", key="Books", label="Books"))
    page = window.stack.currentWidget()
    assert isinstance(page, SearchPage)
    qtbot.waitUntil(lambda: page.model.rowCount() > 0, timeout=5000)
    return page


def _shown(page: SearchPage) -> list[str]:
    return [c.label for i, c in enumerate(page.table.columns) if not page.table.isColumnHidden(i)]


def test_list_columns_offer_every_field(session: KeepSession) -> None:
    columns = {c.key: c for c in list_columns(session.schema, ("books.book",))}
    keys = list(columns)
    assert keys[:4] == ["title", "tags", "authors", "series"]  # card fields first
    assert not columns["authors"].hidden_by_default
    for key in ("link", "description", "isbn", "sources", "year"):
        assert columns[key].hidden_by_default, key
    assert "cover" not in columns  # not shown on pages (detail=False): not offered either


def test_hidden_keys() -> None:
    columns = [
        ResultColumn("title", "Title"),
        ResultColumn("year", "Year"),
        ResultColumn("link", "Link", hidden_by_default=True),
        ResultColumn("isbn", "ISBN", hidden_by_default=True),
    ]
    assert hidden_keys(columns, []) == {"link", "isbn"}
    assert hidden_keys(columns, ["year", "title"], ["link"]) == {"year", "isbn"}  # never title
    assert hidden_keys(columns, ["link"], ["link"]) == {"link", "isbn"}  # hidden wins


def test_a_field_column_is_shown_from_the_menu_and_remembered(
    qtbot: QtBot, session: KeepSession
) -> None:
    page = _books(qtbot, session)
    assert _shown(page) == ["Title", "Author", "Series"]
    actions = {a.text(): a for a in page.table.column_menu().actions()}
    assert {"Link", "Description", "ISBN", "Sources", "Year"} <= set(actions)
    assert not actions["Link"].isChecked()
    actions["Link"].setChecked(True)  # as clicking it in the header's menu does
    assert _shown(page) == ["Title", "Author", "Series", "Link"]
    state = load_ui_state(session.keep.ui_state_path)
    assert state["shown_columns"] == {"view:Books": ["link"]}

    again = _books(qtbot, session)  # a new window for the keep restores the choice
    assert _shown(again) == ["Title", "Author", "Series", "Link"]
    actions = {a.text(): a for a in again.table.column_menu().actions()}
    actions["Link"].setChecked(False)
    assert _shown(again) == ["Title", "Author", "Series"]
    assert load_ui_state(session.keep.ui_state_path)["shown_columns"] == {"view:Books": []}
