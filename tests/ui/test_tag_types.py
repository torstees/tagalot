"""Tags limited to some item types, in the window (#135): the tag manager's Applies to,
the tagging panel hiding tags the selection can't have, and what tagging skipped."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.tag_manager import TagManagerPage
from tagalot.ui.tag_types_dialog import TagTypesDialog
from tagalot.ui.workers import ScanController

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
IMAGE, ARTIST, FONT = "assets2d.image", "assets2d.artist", "assets2d.font"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as session:
        yield session


@pytest.fixture
def window(qtbot: QtBot, session: KeepSession) -> MainWindow:
    window = MainWindow(session, scans=ScanController(QThreadPool()))
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()
    window.navigation.select(NavTarget("tags", label="Tag manager"))
    qtbot.waitUntil(lambda: _page(window).loaded, timeout=5000)
    qtbot.waitUntil(lambda: window.tag_panel.loaded, timeout=5000)
    return window


def _page(window: MainWindow) -> TagManagerPage:
    page = window.stack.currentWidget()
    assert isinstance(page, TagManagerPage)
    return page


def _tag(session: KeepSession, name: str) -> int:
    tree = session.tag_cache.get()
    [tag_id] = [t for t in tree if tree.node(t).name == name]
    return tag_id


def _entity(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None
    return found


def _status(qtbot: QtBot, window: MainWindow, expected: str) -> None:
    def check() -> None:
        assert window.statusBar().currentMessage() == expected

    qtbot.waitUntil(check, timeout=5000)


def test_limiting_a_tag_in_the_tag_manager(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    favorites = _tag(session, "Favorites")  # on forest.png (an image) and the font
    page = _page(window)
    assert page.select_tag(favorites)
    details = page.details
    assert details.types.text() == "Every type"

    offered: list[list[str]] = []
    asked: list[tuple[str, int]] = []

    def choose(dialog: TagTypesDialog) -> frozenset[str]:
        offered.append([box.text() for box in dialog.boxes.values()])
        return frozenset({IMAGE})

    def confirm(name: str, count: int) -> bool:
        asked.append((name, count))
        return True

    monkeypatch.setattr(window, "choose_tag_types", choose)
    monkeypatch.setattr(window, "confirm_tag_type_removal", confirm)
    details.change_types.click()
    _status(qtbot, window, "'Favorites' now applies to Images. Removed it from 1 item.")
    assert offered == [["Archives", "Artists", "Fonts", "Images"]]
    assert asked == [("Favorites", 1)]  # the font
    qtbot.waitUntil(lambda: details.types.text() == "Images", timeout=5000)
    assert session.tag_cache.get().scope(favorites) == frozenset({IMAGE})

    window.tag_actions.undo()
    _status(qtbot, window, "Undid: Change the types 'Favorites' applies to.")
    qtbot.waitUntil(lambda: details.types.text() == "Every type", timeout=5000)


def test_cancelling_the_question_changes_nothing(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    favorites = _tag(session, "Favorites")
    assert _page(window).select_tag(favorites)
    monkeypatch.setattr(window, "choose_tag_types", lambda dialog: frozenset({IMAGE}))
    asked: list[int] = []

    def refuse(name: str, count: int) -> bool:
        asked.append(count)
        return False

    monkeypatch.setattr(window, "confirm_tag_type_removal", refuse)
    _page(window).details.change_types.click()
    qtbot.waitUntil(lambda: asked == [1], timeout=5000)
    assert session.tag_cache.get().scope(favorites) is None


def test_the_panel_hides_tags_the_selection_cant_have(
    window: MainWindow, session: KeepSession
) -> None:
    favorites, sketch = _tag(session, "Favorites"), _tag(session, "Sketch")
    session.tags.set_types(favorites, [IMAGE])
    window.tag_panel.reload()
    model = window.tag_panel.model
    tree = session.tag_cache.get()
    window.tag_panel.set_tree(tree)

    window.tag_panel.set_selection(1, {}, frozenset({ARTIST}))
    assert not model.index_of(favorites).isValid()  # artists can't be favorites
    assert model.index_of(sketch).isValid()
    window.tag_panel.set_selection(2, {}, frozenset({ARTIST, IMAGE}))
    assert model.index_of(favorites).isValid()  # the image can
    window.tag_panel.set_selection(1, {favorites: 1}, frozenset({FONT}))
    assert model.index_of(favorites).isValid()  # kept: the font has it, to remove
    window.tag_panel.set_selection(0, {})
    assert model.index_of(favorites).isValid()  # nothing selected: every tag


def test_tagging_says_what_it_skipped(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    favorites = _tag(session, "Favorites")
    session.tags.set_types(favorites, [IMAGE])
    dusk, kenji = _entity(session, "dusk sky.png"), _entity(session, "Kenji Sato")
    window.tag_actions.apply([dusk, kenji], [favorites], "'Favorites'")
    _status(
        qtbot,
        window,
        "Tagged 1 item with 'Favorites'. Skipped 1 Artist: 'Favorites' is for Images.",
    )
    window.tag_actions.apply([kenji], [favorites], "'Favorites'")
    _status(qtbot, window, "Skipped 1 Artist: 'Favorites' is for Images.")


def test_the_dialog_offers_only_what_the_parent_allows(qtbot: QtBot) -> None:
    plurals = {IMAGE: "Images", ARTIST: "Artists", FONT: "Fonts"}
    dialog = TagTypesDialog("Sky", plurals, None, frozenset({IMAGE, FONT}))
    qtbot.addWidget(dialog)
    assert sorted(b.text() for b in dialog.boxes.values()) == ["Fonts", "Images"]
    assert dialog.every.text() == "Every type its parent allows"
    assert dialog.chosen() is None
    dialog.only.setChecked(True)
    ok = dialog.buttons.button(dialog.buttons.StandardButton.Ok)
    assert not ok.isEnabled()  # at least one type
    dialog.boxes[IMAGE].setChecked(True)
    assert ok.isEnabled()
    assert dialog.chosen() == frozenset({IMAGE})


def test_a_new_sub_tag_is_limited_like_its_parent(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    session.tags.set_types(_tag(session, "Favorites"), [IMAGE])
    dusk, kenji = _entity(session, "dusk sky.png"), _entity(session, "Kenji Sato")
    window.tag_actions.create(["Favorites", "Best"], [dusk, kenji], "Favorites/Best")
    _status(
        qtbot,
        window,
        "Created tag Favorites/Best and tagged 1 item with it. "
        "Skipped 1 Artist: 'Best' is for Images.",
    )
