"""Merging from the Dedupe page's compare pane (#120), on the music demo."""

import shutil
from pathlib import Path

import pytest
from PySide6.QtWidgets import QRadioButton
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.ui.dedupe_view import DedupePage
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.merge_dialog import MergeDialog
from tagalot.ui.navigation import NavTarget
from tests.ui.test_music_views import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


def _two_blues(qtbot: QtBot, window: MainWindow, session: KeepSession) -> DedupePage:
    """A copy of Blue in another folder: one group of identical files, used by two songs,
    compared in the pane."""
    root = Path(session.root_path("music"))
    copy = root / "Copies" / "Blue (copy).mp3"
    copy.parent.mkdir()
    shutil.copy(root / "Jazz Hits" / "02 Blue.mp3", copy)
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    window.navigation.select(NavTarget("dedupe", label="Dedupe"))
    page = window.stack.currentWidget()
    assert isinstance(page, DedupePage)
    qtbot.waitUntil(lambda: page.groups is not None, timeout=5000)
    page.tree.setCurrentIndex(page.model.index(0, 0))
    pane = page.compare_pane
    qtbot.waitUntil(lambda: pane.comparison is not None, timeout=5000)
    return page


def test_merging_from_the_compare_pane(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _two_blues(qtbot, window, session)
    pane = page.compare_pane
    assert pane.merge_button.isVisible()
    assert pane.merge_button.isEnabled()
    assert pane.comparison is not None
    first, second = (i.id for i in pane.comparison.items)
    asked: list[list[tuple[int, str, str]]] = []

    def choose(items: list[tuple[int, str, str]]) -> tuple[int, list[int], dict[str, int]]:
        asked.append(items)
        return first, [second], {}

    window.choose_merge = choose  # type: ignore[method-assign]
    window.open_entity(second)  # the page of the one merged away
    detail = window.stack.currentWidget()
    assert isinstance(detail, DetailPage)
    qtbot.waitUntil(lambda: detail.detail is not None, timeout=5000)
    window.navigation.select(NavTarget("dedupe", label="Dedupe"))
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        pane.merge_button.click()
    assert [(i, t) for i, t, _ in asked[0]] == [(first, "Blue"), (second, "Blue")]
    assert any("Jazz Hits" in where for _, _, where in asked[0])  # to tell them apart
    assert blocker.args == ["Merge 'Blue' into 'Blue'. Edit \u2192 Undo separates them again."]
    # The page refreshed: the group is one item now, with both copies.
    qtbot.waitUntil(
        lambda: _shown(pane.summary.text(), "Every copy belongs to one item, Blue."),
        timeout=5000,
    )
    assert not pane.merge_button.isVisible()
    # Its page now shows the item it was merged into, and says so.
    qtbot.waitUntil(lambda: detail.entity_id == first, timeout=5000)
    assert detail.merged_note.isVisibleTo(detail)
    files = pane.comparison.row("files") if pane.comparison else None
    assert files is not None
    assert files.values[0].count("\n") == 1

    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.tag_actions.undo()
    qtbot.waitUntil(lambda: _shown(pane.summary.text(), "2 items use these copies."), timeout=5000)


def _shown(summary: str, note: str) -> bool:
    """The pane shows a comparison (not still loading it) with this note."""
    return summary.startswith(note) and summary.endswith("open its item.")


def test_the_merge_dialog(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _two_blues(qtbot, window, session)
    assert page.compare_pane.comparison is not None
    first, second = (i.id for i in page.compare_pane.comparison.items)
    session.tags.edit_field(first, "year", 1971)
    session.tags.edit_field(second, "year", 1970)
    dialog = MergeDialog(session, [(first, "Blue", "here"), (second, "Blue", "there")])
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.plan is not None, timeout=5000)
    assert dialog.merge_button.isEnabled()
    assert dialog.keep_id == first
    assert dialog.other_ids == [second]
    # The demo's Blue is tagged, in its album and under its artist; the copy is in Copies.
    assert dialog.summary.text().startswith("'Blue' gains 1 tag, 1 file and 2 containers from")
    assert dialog.conflicts_box.isVisibleTo(dialog)
    assert dialog.choices == {"field:year": first}
    assert not dialog.unplaced.isVisibleTo(dialog)
    [other_year] = [
        b for b in dialog.conflicts_box.findChildren(QRadioButton) if b.text() == "1970"
    ]
    other_year.setChecked(True)
    assert dialog.choices == {"field:year": second}

    dialog.keep_group.button(second).setChecked(True)  # keep the other one
    qtbot.waitUntil(lambda: dialog.plan is not None and dialog.plan.keep[0] == second, timeout=5000)
    assert dialog.other_ids == [first]
    assert dialog.choices == {"field:year": second}  # its own value, by default
