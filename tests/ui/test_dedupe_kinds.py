"""Kind chips on the Dedupe page (#642), on the music demo."""

import shutil
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from tagalot.core.dedupe import NearDuplicates, NearPair
from tagalot.core.search_fields import type_plurals
from tagalot.core.session import KeepSession
from tagalot.ui.dedupe_view import ALL_HIDDEN, DedupePage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tests.ui.test_music_views import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

DEDUPE = NavTarget("dedupe", label="Dedupe")
ALBUM, SONG = "music.album", "music.song"


def _dedupe(qtbot: QtBot, window: MainWindow) -> DedupePage:
    window.navigation.select(DEDUPE)
    page = window.stack.currentWidget()
    assert isinstance(page, DedupePage)
    qtbot.waitUntil(lambda: page.groups is not None and page.pairs is not None, timeout=5000)
    return page


def _names(page: DedupePage) -> list[str]:
    return [g.files[0].relpath.rpartition("/")[2] for g in page.groups or []]


def test_identical_files_hide_by_type(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    root = Path(session.root_path("music"))
    (root / "Copies").mkdir()
    shutil.copy(root / "Jazz Hits" / "02 Blue.mp3", root / "Copies" / "Blue (copy).mp3")
    shutil.copy(root / "Miles Davis/Kind of Blue/cover.jpg", root / "Copies" / "cover.jpg")
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    page = _dedupe(qtbot, window)
    chips = page.identical_chips
    qtbot.waitUntil(lambda: len(page.groups or []) == 2, timeout=5000)
    assert sorted(c.text() for c in chips.chips.values()) == ["JPG 1", "MP3 1"]
    assert chips.isVisibleTo(page)

    chips.toggle(".mp3")
    assert _names(page) == ["cover.jpg"]
    assert page.summary.text().startswith("1 group of identical files")
    assert "1 more of the kinds hidden." in page.summary.text()
    assert window._ui_state["hidden_kinds"]["dedupe:identical"] == [".mp3"]

    chips.toggle(".jpg")
    assert page.groups == []
    assert page.summary.text() == ALL_HIDDEN
    assert not page.check_all_button.isEnabled()

    chips.toggle(".jpg", only=True)  # Ctrl+click: only JPG
    assert _names(page) == ["cover.jpg"]
    chips.toggle(".jpg", only=True)  # again: both
    assert len(page.groups or []) == 2
    assert "dedupe:identical" not in window._ui_state["hidden_kinds"]


def test_similar_items_hide_by_type(qtbot: QtBot, window: MainWindow) -> None:
    window._ui_state.setdefault("hidden_kinds", {})["dedupe:similar"] = [ALBUM]
    page = _dedupe(qtbot, window)
    assert page.similar_chips.hidden == (ALBUM,)  # remembered
    page.all_pairs = NearDuplicates(
        [
            NearPair(SONG, (1, "Blue"), (2, "Blue (live)"), 0.9),
            NearPair(ALBUM, (3, "Jazz Hits"), (4, "Jazz Hits 2"), 0.8),
        ]
    )
    page._show_pairs(page.all_pairs)  # as a search finding them does
    plurals = type_plurals(page.session.schema)
    assert sorted(c.text() for c in page.similar_chips.chips.values()) == sorted(
        [f"{plurals[ALBUM]} 1", f"{plurals[SONG]} 1"]
    )
    assert page.pairs is not None
    assert [p.type_id for p in page.pairs.pairs] == [SONG]
    assert page.tabs.tabText(1) == "Similar items (1 of 2)"

    page.similar_chips.toggle(SONG)
    assert page.pairs is not None
    assert page.pairs.pairs == []
    assert page.similar_summary.text() == ALL_HIDDEN
    assert window._ui_state["hidden_kinds"]["dedupe:similar"] == [ALBUM, SONG]
