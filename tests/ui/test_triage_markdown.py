"""Triage → Markdown (#438): Markdown files whose front matter gives a key twice, beside
the notes that aren't an item's note. On the books demo, whose Field Notes gives ``year``
twice."""

import pytest
from pytestqt.qtbot import QtBot

from tagalot.core.handlers import OPEN, FileToOpen
from tagalot.core.markdown_check import MarkdownFileProblem
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.main_window import MainWindow
from tagalot.ui.triage import MARKDOWN_TAB
from tests.ui.test_file_extra_page import window
from tests.ui.test_triage_pictures import _triage

pytestmark = pytest.mark.gui

__all__ = ["window"]  # fixtures

TWICE = "`year` is given twice (lines 3 and 6); the last, '2021', is used."


def test_the_markdown_tab_lists_front_matter_problems(
    qtbot: QtBot, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    triage = _triage(qtbot, window, 1)  # (its Pictures: the Lighthouse Keeper's cover)
    assert triage.tabs.tabText(MARKDOWN_TAB) == "Markdown (1)"
    triage.tabs.setCurrentIndex(MARKDOWN_TAB)
    [row] = triage.notes.notes
    assert isinstance(row, MarkdownFileProblem)
    assert row.relpath == "Loose/Field Notes.md"
    model = triage.notes
    assert [model.data(model.index(0, c)) for c in range(2)] == ["Loose/Field Notes.md", TWICE]

    opened: list[tuple[FileToOpen, str]] = []
    monkeypatch.setattr(FileOpener, "act", lambda self, file, how: opened.append((file, how)))
    triage.notes_table.selectRow(0)
    triage._open_note(OPEN)
    assert [(f.path.endswith("Field Notes.md"), how) for f, how in opened] == [(True, OPEN)]
