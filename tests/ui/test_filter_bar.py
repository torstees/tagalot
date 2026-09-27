"""Tests for the filter bar and filtering a search page with it."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from tagalot.core.search_spec import SearchSpec, SortKey
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import PATH_SEPARATOR, TagNode, TagTree
from tagalot.ui.filter_bar import TEXT_DELAY_MS, Chip, FilterBar, Filters
from tagalot.ui.search_view import SearchPage

pytestmark = pytest.mark.gui

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"

# id, parent, name
TAGS = [
    (1, None, "Places"),
    (2, 1, "Iceland"),
    (3, 1, "Beach"),
    (4, None, "Events"),
    (5, 4, "Christmas"),
    (6, None, "Ice cream"),
    (7, 4, "Beach"),  # same name as 3
]
TREE = TagTree([TagNode(i, p, n, None, 0) for i, p, n in TAGS], {2: ["Ísland"]})


@pytest.fixture
def bar(qtbot: QtBot) -> Iterator[FilterBar]:
    window = QWidget()  # the suggestion list is a child of the window
    qtbot.addWidget(window)
    bar = FilterBar(window)
    bar.set_tree(TREE)
    window.resize(700, 200)
    window.show()
    yield bar  # the window must stay referenced during the test
    window.close()


def _type_tag(qtbot: QtBot, bar: FilterBar, text: str) -> None:
    bar.tag_edit.setFocus()
    QTest.keyClicks(bar.tag_edit, text)


def _chips(bar: FilterBar) -> list[str]:
    items = [bar.chip_layout.itemAt(i) for i in range(bar.chip_layout.count())]
    chips = [item.widget() for item in items if item is not None]
    return [c.label.text() for c in chips if isinstance(c, Chip)]


def test_typing_suggests_tags(qtbot: QtBot, bar: FilterBar) -> None:
    _type_tag(qtbot, bar, "ice")
    assert bar.tag_edit.popup_visible()
    assert bar.tag_edit.suggestion_ids() == [6, 2]  # "Ice cream" and "Iceland" start with it
    texts = [bar.tag_edit.suggestions.item(r).text() for r in range(2)]
    assert texts[1].startswith("Iceland")
    assert "Places" in texts[1]  # the parent path


def test_enter_includes_and_shift_enter_excludes(qtbot: QtBot, bar: FilterBar) -> None:
    with qtbot.waitSignal(bar.changed) as blocker:
        _type_tag(qtbot, bar, "iceland")
        QTest.keyClick(bar.tag_edit, Qt.Key.Key_Return)
    assert blocker.args == [Filters(include=(2,))]
    assert bar.tag_edit.text() == ""
    assert not bar.tag_edit.popup_visible()
    _type_tag(qtbot, bar, "christ")
    QTest.keyClick(bar.tag_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert bar.filters() == Filters(include=(2,), exclude=(5,))
    assert _chips(bar) == ["Iceland", "not: Christmas"]
    assert bar.chip_area.isVisible()


def test_arrows_choose_a_suggestion_and_escape_closes(qtbot: QtBot, bar: FilterBar) -> None:
    _type_tag(qtbot, bar, "ice")
    QTest.keyClick(bar.tag_edit, Qt.Key.Key_Down)
    QTest.keyClick(bar.tag_edit, Qt.Key.Key_Return)
    assert bar.filters().include == (2,)  # the second suggestion, Iceland
    _type_tag(qtbot, bar, "ice")
    QTest.keyClick(bar.tag_edit, Qt.Key.Key_Up)  # wraps to the last
    QTest.keyClick(bar.tag_edit, Qt.Key.Key_Escape)
    assert not bar.tag_edit.popup_visible()
    assert bar.tag_edit.text() == "ice"
    QTest.keyClick(bar.tag_edit, Qt.Key.Key_Return)  # nothing to choose now
    assert bar.filters().include == (2,)


def test_aliases_and_duplicate_names(qtbot: QtBot, bar: FilterBar) -> None:
    _type_tag(qtbot, bar, "sland")  # only the alias "Ísland" contains it
    assert bar.tag_edit.suggestion_ids() == [2]
    assert "(Ísland)" in bar.tag_edit.suggestions.item(0).text()
    QTest.keyClick(bar.tag_edit, Qt.Key.Key_Return)
    bar.add_tag(3)
    assert _chips(bar) == ["Iceland", f"Places{PATH_SEPARATOR}Beach"]  # tells the Beaches apart


def test_accented_input(qtbot: QtBot, bar: FilterBar) -> None:
    # QTest.keyClicks crashes on non-ASCII text (PySide6 6.11, Windows), so this sets the
    # text the way typing would and emits the same signal.
    bar.tag_edit.setText("ÍSL")
    bar.tag_edit.textEdited.emit("ÍSL")
    assert bar.tag_edit.suggestion_ids() == [2]


def test_no_matches(qtbot: QtBot, bar: FilterBar) -> None:
    _type_tag(qtbot, bar, "zebra")
    assert bar.tag_edit.suggestion_ids() == []
    assert bar.tag_edit.suggestions.item(0).text() == "No matching tags"
    QTest.keyClick(bar.tag_edit, Qt.Key.Key_Return)
    assert bar.filters() == Filters()


def test_a_tag_moves_between_include_and_exclude(qtbot: QtBot, bar: FilterBar) -> None:
    bar.add_tag(2)
    bar.add_tag(5, exclude=True)
    bar.add_tag(2, exclude=True)
    assert bar.filters() == Filters(exclude=(5, 2))
    assert _chips(bar) == ["not: Christmas", "not: Iceland"]
    with qtbot.assertNotEmitted(bar.changed):
        bar.add_tag(2, exclude=True)  # already there


def test_removing_chips_and_clear_all(qtbot: QtBot, bar: FilterBar) -> None:
    bar.add_tag(2)
    bar.add_tag(6)
    bar.add_tag(5, exclude=True)
    chip = next(c for c in bar.findChildren(Chip) if c.tag_id == 6)
    with qtbot.waitSignal(bar.changed) as blocker:
        QTest.mouseClick(chip.close_button, Qt.MouseButton.LeftButton)
    assert blocker.args == [Filters(include=(2,), exclude=(5,))]
    bar.text_edit.setText("glacier")
    QTest.keyClick(bar.text_edit, Qt.Key.Key_Return)
    QTest.mouseClick(bar.clear_button, Qt.MouseButton.LeftButton)
    assert bar.filters() == Filters()
    assert bar.text_edit.text() == ""
    assert not bar.chip_area.isVisible()


def test_text_applies_after_a_pause(qtbot: QtBot, bar: FilterBar) -> None:
    changes: list[Filters] = []
    bar.changed.connect(changes.append)
    QTest.keyClicks(bar.text_edit, "gla")
    assert changes == []  # not on every keystroke
    qtbot.waitUntil(lambda: bool(changes), timeout=TEXT_DELAY_MS * 5)
    assert changes == [Filters(text="gla")]
    QTest.keyClicks(bar.text_edit, "  ")
    QTest.keyClick(bar.text_edit, Qt.Key.Key_Return)
    qtbot.wait(TEXT_DELAY_MS * 2)
    assert changes == [Filters(text="gla")]  # trailing spaces change nothing


def test_chips_before_the_tree_loads_and_for_deleted_tags(qtbot: QtBot) -> None:
    bar = FilterBar()
    qtbot.addWidget(bar)
    bar.add_tag(2)
    bar.add_tag(99, exclude=True)
    assert _chips(bar) == ["…", "not: …"]
    bar.set_tree(TREE)
    assert _chips(bar) == ["Iceland", "not: (deleted tag)"]
    chip = bar.findChildren(Chip)[0]
    assert f"Places{PATH_SEPARATOR}Iceland" in chip.toolTip()


def test_ctrl_f_focuses_the_text_box(qtbot: QtBot, bar: FilterBar) -> None:
    bar.tag_edit.setFocus()
    bar.text_edit.setText("old")
    bar.focus_text()
    assert bar.text_edit.hasFocus()
    assert bar.text_edit.selectedText() == "old"


# --- filtering a search page ---


@pytest.fixture
def demo(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_demo(tmp_path), Settings()) as session:
        yield session


def _titles(page: SearchPage) -> set[str]:
    return {page.model.index(r, 0).data() for r in range(page.model.rowCount())}


def _pick(qtbot: QtBot, page: SearchPage, text: str, *, exclude: bool = False) -> None:
    edit = page.filter_bar.tag_edit
    edit.setFocus()
    QTest.keyClicks(edit, text)
    modifier = Qt.KeyboardModifier.ShiftModifier if exclude else Qt.KeyboardModifier.NoModifier
    with qtbot.waitSignal(page.model.counted, timeout=5000):
        QTest.keyClick(edit, Qt.Key.Key_Return, modifier)


def test_filtering_the_demo_keep(qtbot: QtBot, demo: KeepSession) -> None:
    page = SearchPage(demo, "Search all", SearchSpec(), pool=QThreadPool())
    qtbot.addWidget(page)
    page.show()
    qtbot.waitUntil(lambda: page.status.text() == "9 items", timeout=5000)
    qtbot.waitUntil(lambda: page.filter_bar.tree is not None, timeout=5000)

    _pick(qtbot, page, "places")  # Iceland and Beach, through the hierarchy
    assert page.status.text() == "5 items"
    _pick(qtbot, page, "beach", exclude=True)
    assert _titles(page) == {"glacier.jpg", "geyser.jpg", "northern lights.png"}
    _pick(qtbot, page, "favo")  # every include must match
    assert _titles(page) == {"glacier.jpg"}

    with qtbot.waitSignal(page.model.counted, timeout=5000):
        page.filter_bar.clear()
    assert page.status.text() == "9 items"
    _pick(qtbot, page, "finance")  # an alias of Money
    assert _titles(page) == {"tax 2025.pdf", "März.txt"}
    with qtbot.waitSignal(page.model.counted, timeout=5000):
        page.filter_bar.text_edit.setText("mär")
        QTest.keyClick(page.filter_bar.text_edit, Qt.Key.Key_Return)
    assert _titles(page) == {"März.txt"}


def test_filters_keep_the_sort_and_nothing_found(qtbot: QtBot, demo: KeepSession) -> None:
    spec = SearchSpec(sort=(SortKey("title", descending=True),))
    page = SearchPage(demo, "Search all", spec, pool=QThreadPool())
    qtbot.addWidget(page)
    page.show()  # suggestions only appear in a visible window
    qtbot.waitUntil(lambda: page.filter_bar.tree is not None, timeout=5000)
    _pick(qtbot, page, "iceland")
    assert page.model.index(0, 0).data() == "northern lights.png"  # still Z to A
    assert page.model.spec is not None
    assert page.model.spec.sort == spec.sort
    _pick(qtbot, page, "money")
    assert page.status.text() == "Nothing found"
