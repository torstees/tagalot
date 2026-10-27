"""Icons drawn from Lucide's SVGs and themes' own (#556, DESIGN.md §12 *Icons*): in the
palette's colors, beside the navigation's entries, on an item's page and its tabs, and in
place of a picture."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QSize, QThreadPool
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QTabWidget
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.icons import icon, icon_for, lucide
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavigationPane
from tagalot.ui.workers import ScanController
from tests.ui.test_contents_search import _open, _tabs

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_music_demo(tmp_path), Settings()) as session:
        window = MainWindow(session, scans=ScanController(QThreadPool()))
        qtbot.addWidget(window)
        window.resize(1200, 800)
        window.show()
        yield window
        window.thumbnails.clear()
        window.thumbnails.wait()


def _colors(qicon: QIcon, mode: QIcon.Mode = QIcon.Mode.Normal) -> set[str]:
    """The opaque colors the icon is drawn in."""
    image = qicon.pixmap(QSize(24, 24), mode).toImage()
    return {
        image.pixelColor(x, y).name()
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() == 255
    }


def test_icons_are_drawn_in_the_palettes_colors(qapp: QApplication) -> None:
    old = qapp.palette()
    palette = QPalette(old)
    palette.setColor(QPalette.ColorRole.Text, QColor("#123456"))
    qapp.setPalette(palette)
    try:
        assert _colors(lucide("film")) == {"#123456"}
        # Selected: whichever reads on the selection (tests/ui/test_selection.py).
    finally:
        qapp.setPalette(old)
    assert _colors(lucide("film")) == {old.color(QPalette.ColorRole.Text).name()}


def test_unknown_names_get_the_generic_icon(tmp_path: Path) -> None:
    generic = lucide("file").cacheKey()
    assert lucide("no-such-icon").cacheKey() == generic
    assert icon(None).cacheKey() == generic
    assert icon(str(tmp_path / "gone.svg")).cacheKey() == generic
    assert icon_for("entity").cacheKey() == generic
    assert icon_for("audio").cacheKey() == lucide("file-audio").cacheKey()
    assert icon_for("music").cacheKey() == lucide("music").cacheKey()  # a type's icon


def test_a_themes_own_svg(tmp_path: Path) -> None:
    path = tmp_path / "wand.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" stroke="currentColor">'
        '<rect x="0" y="0" width="24" height="24" fill="currentColor"/></svg>',
        encoding="utf-8",
    )
    drawn = icon(str(path))
    assert drawn.cacheKey() != lucide("file").cacheKey()
    assert len(_colors(drawn)) == 1


def test_the_navigation_shows_each_views_type_icon(qtbot: QtBot) -> None:
    pane = NavigationPane(["Movies", "Odd"], view_icons={"Movies": "film", "Odd": None})
    qtbot.addWidget(pane)

    def icon_of(section: str, label: str) -> int:
        model = pane.model()
        for row in range(model.rowCount()):
            heading = model.index(row, 0)
            for child in range(model.rowCount(heading)):
                index = model.index(child, 0, heading)
                if index.data() == label:
                    found = index.data(1)  # DecorationRole
                    assert isinstance(found, QIcon), label
                    return found.cacheKey()
        raise AssertionError(f"no {label!r} in {section}")

    assert icon_of("searches", "Movies") == lucide("film").cacheKey()
    assert icon_of("searches", "Odd") == lucide("file").cacheKey()
    assert icon_of("library", "Dashboard") == lucide("layout-dashboard").cacheKey()
    assert icon_of("tools", "Tag manager") == lucide("tags").cacheKey()


def test_the_window_uses_the_themes_icons(qtbot: QtBot, window: MainWindow) -> None:
    pane = window.navigation
    model = pane.model()
    searches = model.index(1, 0)
    icons = {
        model.index(r, 0, searches).data(): model.index(r, 0, searches).data(1).cacheKey()
        for r in range(model.rowCount(searches))
    }
    assert icons["Albums"] == lucide("disc").cacheKey()
    assert icons["Songs"] == lucide("music").cacheKey()
    assert window.back_action.icon().cacheKey() == lucide("arrow-left").cacheKey()


def test_an_items_page_shows_its_types_icon(qtbot: QtBot, window: MainWindow) -> None:
    assert window.session is not None
    detail = _open(qtbot, window, window.session, "Kind of Blue")
    assert not detail.type_icon.pixmap().isNull()
    qtbot.waitUntil(lambda: _tabs(detail) == ["Songs (3)", "Scans (3)"], timeout=5000)
    tabs = detail.findChild(QTabWidget, "bottom_tabs")
    assert tabs is not None
    assert tabs.tabIcon(0).cacheKey() == lucide("music").cacheKey()
    assert tabs.tabIcon(1).cacheKey() == lucide("images").cacheKey()
