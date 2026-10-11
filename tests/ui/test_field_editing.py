"""Editing fields in place on detail pages, with undo (#93)."""

from datetime import UTC, date, datetime

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QToolButton, QWidget
from pytestqt.qtbot import QtBot

from tagalot.core.session import KeepSession
from tagalot.ui.detail_view import DetailPage
from tagalot.ui.field_editor import (
    BOX_WIDTH,
    EditableValue,
    ParseError,
    editor_text,
    parse_value,
)
from tagalot.ui.main_window import MainWindow
from tests.ui.test_contents_search import _open, session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

FONT = "Aileron-Regular.ttf"


def _field(page: DetailPage, name: str) -> EditableValue:
    found = page.findChild(EditableValue, f"field_{name}")
    assert isinstance(found, EditableValue), name
    return found


def _double_click(widget: EditableValue) -> None:
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonDblClick,
        QPointF(2, 2),
        QPointF(2, 2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget.label, event)


def _type(widget: EditableValue, text: str) -> None:
    widget.box.setText(text)
    QTest.keyClick(widget.box, Qt.Key.Key_Return)


# --- parsing what the user types ---


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("  hello ", str, "hello"),
        ("", int, None),
        ("1,995", int, 1995),
        ("7.5", float, 7.5),
        ("1995-12-15", date, date(1995, 12, 15)),
        ("2026-01-02 03:04", datetime, datetime(2026, 1, 2, 3, 4)),
    ],
)
def test_parse_value(text: str, kind: type, value: object) -> None:
    assert parse_value(text, kind) == value


@pytest.mark.parametrize(
    ("text", "kind", "message"),
    [
        ("soon", int, "whole number"),
        ("1.5.2", float, "number"),
        ("15/12/1995", date, "YYYY-MM-DD"),
    ],
)
def test_parse_errors(text: str, kind: type, message: str) -> None:
    with pytest.raises(ParseError, match=message):
        parse_value(text, kind)


def test_editor_text_round_trips() -> None:
    moment = datetime(2026, 1, 2, 3, 4).astimezone(UTC)
    assert parse_value(editor_text(moment), datetime).astimezone(UTC) == moment
    assert editor_text(date(1995, 12, 15)) == "1995-12-15"
    assert editor_text(None) == ""


def test_a_value_that_doesnt_parse_stays_in_the_box(qtbot: QtBot) -> None:
    value = EditableValue(1994, int, editable=True, label="Year")
    qtbot.addWidget(value)
    committed: list[object] = []
    value.committed.connect(committed.append)
    value.start_editing()
    _type(value, "soon")
    assert value.editing()
    assert "whole number" in value.box.toolTip()
    _type(value, "1995")
    assert not value.editing()
    assert committed == [1995]
    assert value.label.text() == "1995"


def test_escape_cancels_and_read_only_values_dont_edit(qtbot: QtBot) -> None:
    value = EditableValue("Aileron", str, editable=True, label="Family")
    qtbot.addWidget(value)
    committed: list[object] = []
    value.committed.connect(committed.append)
    value.start_editing()
    value.box.setText("Other")
    QTest.keyClick(value.box, Qt.Key.Key_Escape)
    assert not value.editing()
    assert value.label.text() == "Aileron"
    fixed = EditableValue(".ttf", str, editable=False, label="Extension")
    qtbot.addWidget(fixed)
    _double_click(fixed)
    assert not fixed.editing()
    assert committed == []


# --- on a detail page ---


def test_editing_a_field_marks_it_and_undo_puts_it_back(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _open(qtbot, window, session, FONT)
    family = _field(page, "family")
    assert family.label.text() == "Aileron"
    assert family.marker.isHidden()
    _double_click(family)
    assert family.editing()
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        _type(family, "Aileron Sans")
    assert blocker.args == [f"Set Family of '{FONT}' to 'Aileron Sans'."]
    qtbot.waitUntil(lambda: not _field(page, "family").marker.isHidden(), timeout=5000)
    assert _field(page, "family").label.text() == "Aileron Sans"
    assert window.undo_action.text().startswith("Undo")

    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.undo_action.trigger()
    qtbot.waitUntil(lambda: _field(page, "family").label.text() == "Aileron", timeout=5000)
    assert _field(page, "family").marker.isHidden()


def test_renaming_from_the_title(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _open(qtbot, window, session, FONT)
    _double_click(page.title_value)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        _type(page.title_value, "Aileron (regular)")
    qtbot.waitUntil(lambda: not page.title_value.marker.isHidden(), timeout=5000)
    assert page.title.text() == "Aileron (regular)"


def test_file_facts_are_read_only(qtbot: QtBot, window: MainWindow, session: KeepSession) -> None:
    page = _open(qtbot, window, session, FONT)
    assert _field(page, "family").editable
    for name in ("extension", "folder", "size", "modified", "artist"):
        assert not _field(page, name).editable, name


# --- extra fields (#95) ---


def _extra(page: DetailPage, name: str) -> EditableValue | None:
    found = page.findChild(EditableValue, f"extra_{name}")
    return found if isinstance(found, EditableValue) else None


def _section_order(page: DetailPage) -> list[str]:
    layout = page._sections
    names = []
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget() if item is not None else None
        if widget is not None:
            names.append(widget.objectName())
    return names


def test_adding_editing_and_removing_extra_fields(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _open(qtbot, window, session, FONT)
    assert _section_order(page)[:2] == ["section_fields", "section_extra"]
    page.extra_name.setText("Licence")
    page.extra_value.setText("SIL Open Font License")
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        QTest.keyClick(page.extra_value, Qt.Key.Key_Return)
    assert blocker.args == [f"Add 'Licence' to '{FONT}'."]
    qtbot.waitUntil(lambda: _extra(page, "Licence") is not None, timeout=5000)
    licence = _extra(page, "Licence")
    assert licence is not None
    assert licence.label.text() == "SIL Open Font License"

    _double_click(licence)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        _type(licence, "OFL 1.1")
    qtbot.waitUntil(
        lambda: (value := _extra(page, "Licence")) is not None and value.label.text() == "OFL 1.1",
        timeout=5000,
    )

    remove = next(b for b in page.findChildren(QToolButton) if b.toolTip() == "Remove 'Licence'")
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as removed:
        remove.click()
    assert removed.args == [f"Remove 'Licence' from '{FONT}'."]
    qtbot.waitUntil(lambda: _extra(page, "Licence") is None, timeout=5000)

    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.undo_action.trigger()
    qtbot.waitUntil(lambda: _extra(page, "Licence") is not None, timeout=5000)


def test_a_type_without_fields_shows_extra_fields_first(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _open(qtbot, window, session, "Kenji Sato")
    assert _section_order(page)[0] == "section_extra"


def test_a_duplicate_extra_name_is_refused(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    page = _open(qtbot, window, session, FONT)
    for _ in range(2):
        page.extra_name.setText("Licence")
        page.extra_value.setText("OFL")
        page._add_extra()
        qtbot.waitUntil(lambda: window.tag_actions.busy == 0, timeout=5000)
    assert "already has a field named 'Licence'" in window.statusBar().currentMessage()


@pytest.mark.parametrize("width", [1200, 700])
def test_the_text_box_isnt_squeezed(qtbot: QtBot, width: int) -> None:
    """A value followed by a stretch (an Extra field's row) got the box's narrow hint, so
    the box was clipped and what was typed couldn't be seen (#380 review)."""
    url = "https://aurora.example/studio/about-us/contact-and-commissions"
    row = QWidget()
    layout = QHBoxLayout(row)
    value = EditableValue(url, str, editable=True, label="website")
    layout.addWidget(value)
    layout.addWidget(QToolButton())
    layout.addStretch(1)
    qtbot.addWidget(row)
    row.resize(width, 40)
    row.show()
    value.start_editing()

    def box_shown() -> None:
        assert value.stack.width() >= value.box.width(), (
            f"the box is {value.box.width()} px but only {value.stack.width()} px show"
        )

    qtbot.waitUntil(box_shown, timeout=2000)
    assert value.box.width() >= BOX_WIDTH
    text = value.box.fontMetrics().horizontalAdvance(url)
    if width > text + 200:  # room for all of it: all of it shows
        assert value.box.width() > text


def test_a_url_field_shows_a_link_that_opens_the_browser(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("tagalot.ui.field_editor.open_web_address", opened.append)
    url = "https://example.com/a?b=1&c=<2>"
    value = EditableValue(url, str, editable=True, label="Link", display="url")
    qtbot.addWidget(value)
    assert value.label.textFormat() == Qt.TextFormat.RichText
    assert value.label.text() == (
        '<a href="https://example.com/a?b=1&amp;c=&lt;2&gt;">'
        "https://example.com/a?b=1&amp;c=&lt;2&gt;</a>"
    )
    value.label.linkActivated.emit(url)  # as a click on the link does
    assert opened == [url]
    value.start_editing()
    assert value.box.text() == url  # still edited as text
    value.show_value("not a web address", False)
    assert value.label.textFormat() == Qt.TextFormat.PlainText
    assert value.label.text() == "not a web address"
    value.show_value("<b>plain</b>", False)  # other fields never render markup
    assert value.label.textFormat() == Qt.TextFormat.PlainText
