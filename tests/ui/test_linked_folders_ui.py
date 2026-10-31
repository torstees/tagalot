"""The Dedupe page's Linked folders tab (#601), on the music demo: a folder copied
elsewhere, as a share shows a linked folder, suggested, previewed, and linked; and files
that differ blocking it until Check again finds them the same."""

import shutil
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from tagalot.core.handlers import OPEN
from tagalot.core.linked_folders import FolderDifference
from tagalot.core.not_duplicates import folder_entry
from tagalot.core.session import KeepSession
from tagalot.ui.dedupe_view import FOLDERS_TAB, DedupePage
from tagalot.ui.main_window import MainWindow
from tagalot.ui.navigation import NavTarget
from tagalot.ui.triage import LINKS_TAB, TriagePage
from tests.core.test_shortcuts import make_lnk
from tests.ui.test_music_views import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


def _linked_jazz(
    qtbot: QtBot, window: MainWindow, session: KeepSession, *, edit: bool = False
) -> DedupePage:
    """Jazz Hits copied into Copies (one file changed with ``edit``), scanned, and the
    pair selected on the Linked folders tab."""
    root = Path(session.root_path("music"))
    shutil.copytree(root / "Jazz Hits", root / "Copies" / "Jazz Hits")
    if edit:
        changed = root / "Copies" / "Jazz Hits" / "02 Blue.mp3"
        changed.write_bytes(changed.read_bytes()[:-10] + b"0123456789")
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    window.navigation.select(NavTarget("dedupe", label="Dedupe"))
    page = window.stack.currentWidget()
    assert isinstance(page, DedupePage)
    page.tabs.setCurrentIndex(FOLDERS_TAB)
    qtbot.waitUntil(lambda: bool(page.folder_pairs), timeout=5000)
    page.folders_table.setCurrentIndex(page.folders.index(0, 0))
    return page


def _sections(page: DedupePage) -> list[str]:
    return [page.preview.item(n, 0).text() for n in range(page.preview.rowCount())]


def test_linking_folders_from_the_dedupe_page(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _linked_jazz(qtbot, window, session)
    pair = page._current_folders()
    assert pair is not None
    assert (pair.first.relpath, pair.other.relpath) == ("Copies/Jazz Hits", "Jazz Hits")
    assert any(s.startswith("Match (") for s in _sections(page))
    assert page.folder_warning.text() == ""
    assert not page.check_folders_button.isVisible()
    assert page.link_folders_button.isEnabled()
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        page.link_folders_button.click()
    assert blocker.args == ["Link folders Jazz Hits. Edit → Undo separates them again."]
    # The files are places of one file now, not duplicates; both lists refresh on their own.
    qtbot.waitUntil(lambda: page.folder_pairs == [] and page.groups == [], timeout=5000)


def test_files_that_differ_block_linking_until_checked(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _linked_jazz(qtbot, window, session, edit=True)
    pair = page._current_folders()
    assert pair is not None
    [differ] = pair.differ
    assert _sections(page)[0] == "Differ (1)"
    assert "1 file differs" in page.folder_warning.text()
    assert not page.link_folders_button.isEnabled()
    assert page.check_folders_button.isEnabled()
    key = folder_entry(pair)
    page.check_folders_button.click()
    qtbot.waitUntil(lambda: key in page.folder_checks, timeout=5000)
    assert page.folder_checks[key] == set()  # they really differ
    assert page.preview.item(0, 0).child(0, 1).text() == "differs"
    assert not page.link_folders_button.isEnabled()

    # Had the full read found them the same (a network glitch), linking would be allowed.
    page.folder_checks[key] = {(differ.first_id, differ.other_id)}
    page._preview_folders()
    assert page.link_folders_button.isEnabled()
    assert page.preview.item(0, 0).child(0, 1).text() == "same, read whole"
    assert "same, read whole" in page.folder_warning.text()


def test_a_file_that_differs_later_is_listed_in_triage(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _linked_jazz(qtbot, window, session)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        page.link_folders_button.click()
    root = Path(session.root_path("music"))
    for folder, tail in (("Jazz Hits", b""), ("Copies/Jazz Hits", b"0123456789")):
        added = root / folder / "03 Late.mp3"
        added.write_bytes((root / "Jazz Hits" / "02 Blue.mp3").read_bytes() + tail)
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    window.navigation.select(NavTarget("triage", label="Triage"))
    triage = window.stack.currentWidget()
    assert isinstance(triage, TriagePage)
    triage.refresh()
    qtbot.waitUntil(lambda: triage.counts is not None and triage.counts.links == 1, timeout=5000)
    assert triage.tabs.tabText(LINKS_TAB) == "Links (1)"
    [row] = triage.links.rows
    assert isinstance(row, FolderDifference)
    assert row.relpath == "Jazz Hits/03 Late.mp3"
    assert row.first_relpath == "Copies/Jazz Hits/03 Late.mp3"


def test_a_broken_shortcut_is_listed_in_triage(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    root = Path(session.root_path("music"))
    (root / "Jazz Hits" / "Gone.mp3.lnk").write_bytes(make_lnk(relative="Gone.mp3"))
    with qtbot.waitSignal(window.scans.finished, timeout=10_000):
        window.scan_now()
    window.navigation.select(NavTarget("triage", label="Triage"))
    triage = window.stack.currentWidget()
    assert isinstance(triage, TriagePage)
    triage.refresh()
    qtbot.waitUntil(lambda: triage.counts is not None and triage.counts.links == 1, timeout=5000)
    assert triage.links.data(triage.links.index(0, 1)) == "Jazz Hits/Gone.mp3.lnk"
    problem = triage.links.data(triage.links.index(0, 2))
    assert isinstance(problem, str)
    assert problem.startswith("Broken: pointed to ")
    assert problem.endswith("Gone.mp3")
    triage.links_table.selectRow(0)
    with qtbot.waitSignal(triage.message, timeout=1000) as said:
        triage._open_link(OPEN, other=False)  # nothing to open
    assert said.args[0].startswith("Its target is gone")
