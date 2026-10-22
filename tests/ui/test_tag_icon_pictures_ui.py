"""Tag icons that are pictures (#507): chosen in the Tag manager, drawn in tag trees (an
animated GIF plays there), on items' badges, and read from an SVG.

The demo keep's Favorites has a star, People no icon (Family is under it).
"""

import io
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QSize, Qt
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.tag_pictures import ICON_SIZE, prepare_icon
from tagalot.core.tags import PictureData
from tagalot.ui.badges import BADGES_ROLE
from tagalot.ui.icon_pictures import read_icon_file
from tagalot.ui.models.results import BADGES
from tagalot.ui.models.tag_tree import ICON_SLOT, SWATCH_SLOT
from tests.ui.test_grid import _close, _grid_page, _page, _row, _window, keep_dir, session
from tests.ui.test_tag_icons_ui import _manager, _select, _wait_status

pytestmark = pytest.mark.gui

__all__ = ["keep_dir", "session"]  # fixtures


def _png(size: tuple[int, int] = (40, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, (30, 120, 200, 255)).save(out, "PNG")
    return out.getvalue()


def _gif() -> bytes:
    frames = [Image.new("RGB", (16, 16), (i * 80, 0, 0)) for i in range(3)]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=30, loop=0)
    return out.getvalue()


def _tag(session: KeepSession, name: str) -> int:
    tree = session.tag_cache.get()
    [tag_id] = [t for t in tree if tree.node(t).name == name]
    return tag_id


def test_choosing_a_picture_as_an_icon(
    qtbot: QtBot, session: KeepSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "logo.png"
    source.write_bytes(_png())
    window = _window(qtbot, session)
    asked: list[bool] = []

    def choose(icon: bool = False) -> str:
        asked.append(icon)
        return str(source)

    monkeypatch.setattr(window, "choose_tag_picture_file", choose)
    page = _manager(qtbot, window)
    details = page.details
    people = _tag(session, "People")
    _select(page, session, "People")
    assert not details.clear_icon.isEnabled()  # nothing to clear
    details.choose_icon.click()
    _wait_status(qtbot, window, "Set a picture as the icon of 'People'.")
    assert asked == [True]  # SVGs offered too
    assert session.tag_cache.get().icon_picture(people) is not None
    qtbot.waitUntil(lambda: details.icon_picture.isVisibleTo(details), timeout=5000)
    assert details.clear_icon.isEnabled()

    # The Tags panel draws it beside the name, with room for a swatch.
    panel = window.tag_panel
    qtbot.waitUntil(
        lambda: panel.model.tree is not None and panel.model.tree.icon_picture(people) is not None,
        timeout=5000,
    )
    assert panel.view.iconSize() == QSize(SWATCH_SLOT + ICON_SLOT, ICON_SLOT)
    index = panel.model.index_of(people)
    assert index.data(Qt.ItemDataRole.DecorationRole) is not None

    # Family shows it, from People.
    qtbot.waitUntil(
        lambda: page.model.tree is not None and page.model.tree.icon_picture(people) is not None
    )
    _select(page, session, "Family")
    assert "<img src=" in details.icon_note.text()
    assert details.icon_note.text().endswith(" from 'People'")

    _select(page, session, "People")
    details.clear_icon.click()
    qtbot.waitUntil(lambda: session.tag_cache.get().own_icon(people) is None, timeout=5000)
    qtbot.waitUntil(lambda: panel.model.decoration_size() is None, timeout=5000)
    _close(window)


def test_picture_badges_on_items(qtbot: QtBot, session: KeepSession) -> None:
    favorites = _tag(session, "Favorites")
    picture = prepare_icon(_png())
    session.tags.set_icon_picture(favorites, picture)
    window = _window(qtbot, session)
    page = _page(window)
    column = [c.key for c in page.model.columns].index(BADGES)

    def badges() -> tuple[object, ...]:
        found = page.model.index(_row(page, "glacier.jpg"), column).data(BADGES_ROLE)
        return tuple(found or ())

    qtbot.waitUntil(lambda: picture in badges(), timeout=5000)
    assert all(isinstance(b, str | PictureData) for b in badges())
    assert not page.table.grab().isNull()  # the column drew it
    grid = _grid_page(qtbot, window)
    found = grid.model.row(_row(grid, "glacier.jpg"))
    assert found is not None
    assert picture in found[1][BADGES].icons
    assert not grid.grid.grab().isNull()  # and the card
    _close(window)


def test_animated_icons_play_in_tag_trees(qtbot: QtBot, session: KeepSession) -> None:
    favorites = _tag(session, "Favorites")
    session.tags.set_icon_picture(favorites, prepare_icon(_gif()))
    window = _window(qtbot, session)
    model = window.tag_panel.model
    qtbot.waitUntil(lambda: favorites in model._movies, timeout=5000)
    first = model.index_of(favorites)

    with qtbot.waitSignal(
        model.dataChanged,
        timeout=5000,
        check_params_cb=lambda top, _bottom, roles: (
            top == first and Qt.ItemDataRole.DecorationRole in roles
        ),
    ):
        pass
    session.tags.set_icon(favorites, "⭐")  # an emoji again: it stops
    model.set_tree(session.tag_cache.get())
    assert favorites not in model._movies
    _close(window)


def test_an_svg_icon_is_drawn_to_a_picture(tmp_path: Path) -> None:
    svg = tmp_path / "badge.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
        '<rect width="20" height="10" fill="#c00"/></svg>',
        encoding="utf-8",
    )
    icon = read_icon_file(str(svg))
    assert icon.data.startswith(b"\x89PNG")
    assert (icon.width, icon.height) == (ICON_SIZE, ICON_SIZE)  # drawn centered on a square
