"""The keep's own fields on a list page (#583): a column (shown when it says so) with
values, a sort, a card line, and + Field."""

from typing import Any

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import Entity
from tagalot.core.own_fields import OwnField
from tagalot.core.search_spec import SortKey
from tagalot.core.session import KeepSession
from tagalot.ui.result_table import list_columns
from tagalot.ui.search_view import SearchPage
from tests.ui.test_field_columns import _books, _shown, session

pytestmark = pytest.mark.gui

__all__ = ["session"]  # the fixture

BOOK = "books.book"
PAGES = OwnField(BOOK, "pages", "Pages", "integer", column=True, card=True)
SHOP = OwnField(BOOK, "shop", "Shop", search="choice")


def _cell(page: SearchPage, title: str, key: str) -> Any:
    model = page.model
    column = next(i for i, c in enumerate(model.columns) if c.key == key)
    for row in range(model.rowCount()):
        hit = model.hit(row)
        if hit is not None and hit.title == title:
            return model.data(model.index(row, column), Qt.ItemDataRole.DisplayRole)
    raise LookupError(title)


def _title(page: SearchPage, row: int) -> str:
    hit = page.model.hit(row)
    return hit.title if hit is not None else ""


@pytest.fixture
def fields(session: KeepSession) -> KeepSession:
    session.add_own_field(PAGES)
    session.add_own_field(SHOP)
    with session.reader.connect() as conn:
        ids = dict(conn.execute(select(Entity.title, Entity.id).where(Entity.type == BOOK)).all())
    for n, entity_id in enumerate(sorted(ids.values())):
        session.tags.edit_field(entity_id, PAGES.key, 100 + n)
    session.tags.edit_field(ids["The Colour of Magic"], SHOP.key, "Kobo")
    return session


def test_list_columns_add_them_after_the_themes(fields: KeepSession) -> None:
    columns = {c.key: c for c in list_columns(fields.schema, (BOOK,), own=fields.own_fields)}
    assert list(columns)[-2:] == ["own:pages", "own:shop"]
    assert not columns["own:pages"].hidden_by_default  # column = true
    assert columns["own:pages"].numeric
    assert columns["own:shop"].hidden_by_default
    assert "own:pages" not in {c.key for c in list_columns(fields.schema, (BOOK,))}


def test_a_list_shows_sorts_and_filters_by_them(qtbot: QtBot, fields: KeepSession) -> None:
    page = _books(qtbot, fields)
    assert "Pages" in _shown(page)
    assert "Shop" not in _shown(page)
    qtbot.waitUntil(lambda: _cell(page, "The Colour of Magic", "own:pages") not in (None, ""))
    assert int(_cell(page, "The Colour of Magic", "own:pages")) >= 100

    menu = [a.text() for a in page.filter_bar.field_menu.actions()]
    assert {"Pages", "Shop"} <= set(menu)

    column = next(i for i, c in enumerate(page.model.columns) if c.key == "own:pages")
    page.model.sort(column, Qt.SortOrder.DescendingOrder)
    spec = page.model.spec
    assert spec is not None
    assert spec.sort[0] == SortKey("own:pages", descending=True)

    def most_first() -> bool:
        if page.model.rowCount() < 2:
            return False
        pages = [_cell(page, _title(page, r), "own:pages") for r in (0, 1)]
        return all(pages) and int(pages[0]) > int(pages[1])

    qtbot.waitUntil(most_first, timeout=5000)


def test_a_card_line_shows_them(qtbot: QtBot, fields: KeepSession) -> None:
    page = _books(qtbot, fields)
    lines = page.card_lines((BOOK,))[BOOK]
    assert ("own:pages", "Pages") in lines
    assert ("own:shop", "Shop") not in lines  # card = false
    assert ("own:shop", "Shop") in page.card_fields((BOOK,))
