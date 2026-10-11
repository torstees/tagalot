"""Every field as a list column (#314): card fields shown, the others hidden until the user
shows them from the header menu, and remembered per view. On the books demo, whose Link,
Description, and ISBN aren't card fields."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import QHeaderView
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.ui_state import load_ui_state
from tagalot.ui.main_window import MainWindow
from tagalot.ui.models.results import ResultColumn
from tagalot.ui.navigation import NavTarget
from tagalot.ui.result_table import (
    COLUMN_WIDTH,
    TITLE_MAX_WIDTH,
    TITLE_MIN_WIDTH,
    ResultTable,
    hidden_keys,
    list_columns,
    make_result_table,
    set_column_widths,
)
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
    assert keys[:5] == ["title", "badges", "tags", "authors", "series"]  # card fields first
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


def _table(qtbot: QtBot, width: int, hidden: tuple[str, ...] = ()) -> ResultTable:
    """A table of a title and three fields, ``width`` pixels wide."""
    columns = [ResultColumn(k, k.title()) for k in ("title", "author", "series", "description")]
    table = make_result_table()
    qtbot.addWidget(table)
    model = QStandardItemModel(3, len(columns), table)
    table.setModel(model)
    set_column_widths(table, columns)
    table.set_columns(columns, hidden)
    table.resize(width, 300)
    table.show()
    qtbot.waitExposed(table)
    return table


def _sizes(table: ResultTable) -> list[int]:
    header = table.horizontalHeader()
    return [header.sectionSize(i) for i in range(header.count()) if not header.isSectionHidden(i)]


def test_every_column_can_be_resized(qtbot: QtBot) -> None:
    """The title used to stretch, which Qt never lets the user resize (#413)."""
    table = _table(qtbot, 900)
    header = table.horizontalHeader()
    assert all(
        header.sectionResizeMode(i) == QHeaderView.ResizeMode.Interactive
        for i in range(header.count())
    )


WIDTHS = {
    "narrow: the title takes what's left": (800, ()),
    "very narrow: the title's least": (500, ()),
    "wide: the last column takes the rest": (1400, ()),
    "a column hidden": (800, ("description",)),
}


@pytest.mark.parametrize("case", WIDTHS.values(), ids=WIDTHS.keys())
def test_the_title_fits_the_spare_width(qtbot: QtBot, case: tuple[int, tuple[str, ...]]) -> None:
    width, hidden = case
    table = _table(qtbot, width, hidden)
    sizes, viewport = _sizes(table), table.viewport().width()
    others = COLUMN_WIDTH * (len(sizes) - 1)
    assert sizes[0] == max(TITLE_MIN_WIDTH, min(TITLE_MAX_WIDTH, viewport - others))
    if sizes[0] < viewport - others:  # room to spare: the last column fills it
        assert sum(sizes) == viewport
    else:
        assert sizes[1:] == [COLUMN_WIDTH] * (len(sizes) - 1)


def test_a_title_the_user_resized_keeps_its_width(qtbot: QtBot) -> None:
    table = _table(qtbot, 900)
    header = table.horizontalHeader()
    header.resizeSection(0, 150)  # as dragging its edge does
    assert not table.title_fitted
    assert sum(_sizes(table)) == table.viewport().width()  # Description takes the room
    table.resize(700, 300)
    qtbot.waitUntil(lambda: table.viewport().width() < 700)
    assert header.sectionSize(0) == 150
    table.set_columns(table.columns, ("series",))  # showing or hiding a column keeps it too
    assert header.sectionSize(0) == 150


def test_the_tree_header_offers_the_columns_too(qtbot: QtBot, session: KeepSession) -> None:
    """Right-clicking the tree's header shows and hides columns, as the list's does."""
    page = _books(qtbot, session)
    page.tree_button.click()
    header = page.tree.header()
    assert header.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    description = [c.key for c in page.table.columns].index("description")
    assert header.isSectionHidden(description)
    {a.text(): a for a in page.column_menu().actions()}["Description"].setChecked(True)
    assert not header.isSectionHidden(description)
    assert header.sectionSize(description) >= COLUMN_WIDTH
    assert _shown(page)[-1] == "Description"  # and in the list
    {a.text(): a for a in page.column_menu().actions()}["Description"].setChecked(False)
    assert header.isSectionHidden(description)
