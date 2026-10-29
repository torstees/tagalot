"""The keep's own fields in the window (#581): edited on an item's page with undo, and
added, changed, and removed in Keep configuration's Fields tab."""

from dataclasses import replace

import pytest
from PySide6.QtWidgets import QDialogButtonBox
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.keep import load_keep_config
from tagalot.core.models import Entity
from tagalot.core.own_fields import OwnField, OwnFieldError
from tagalot.core.session import KeepSession
from tagalot.ui.keep_config import KeepConfigWindow
from tagalot.ui.main_window import MainWindow
from tagalot.ui.own_fields import OwnFieldDialog, OwnFieldsTab
from tests.ui.test_contents_search import _id, _open, session, window
from tests.ui.test_field_editing import _double_click, _field, _type

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

FONT = "Aileron-Regular.ttf"


def _type_of(session: KeepSession, title: str) -> str:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.type).where(Entity.id == _id(session, title)))
    assert found is not None
    return found


def test_an_own_field_is_edited_on_the_page_with_undo(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    type_id = _type_of(session, FONT)
    session.add_own_field(OwnField(type_id, "licence_paid", "Licence paid", "integer"))
    page = _open(qtbot, window, session, FONT)
    paid = _field(page, "own:licence_paid")
    assert paid.label.text() in ("", "—")
    assert paid.editable
    _double_click(paid)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        _type(paid, "25")
    assert blocker.args == [f"Set Licence paid of '{FONT}' to 25."]
    qtbot.waitUntil(lambda: not _field(page, "own:licence_paid").marker.isHidden(), timeout=5000)
    assert _field(page, "own:licence_paid").label.text() == "25"

    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000):
        window.undo_action.trigger()
    qtbot.waitUntil(lambda: _field(page, "own:licence_paid").label.text() != "25", timeout=5000)


def _tab(qtbot: QtBot, session: KeepSession) -> tuple[KeepConfigWindow, OwnFieldsTab]:
    config = KeepConfigWindow(session)
    qtbot.addWidget(config)
    return config, config.fields_tab


def test_the_fields_tab_adds_changes_and_removes_fields(
    qtbot: QtBot, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    type_id = _type_of(session, FONT)
    config, tab = _tab(qtbot, session)
    assert tab.selected() is None
    assert not tab.remove_button.isEnabled()

    pages = OwnField(type_id, "designer", "Designer")
    monkeypatch.setattr(tab, "ask_field", lambda current, type_id: pages)
    with qtbot.waitSignal(config.changed, timeout=5000) as blocker:
        tab.add_field()
    assert blocker.args == ["Added the field 'Designer'; set it on an item's page."]
    qtbot.waitUntil(lambda: tab.selected() == pages, timeout=5000)
    assert list(load_keep_config(session.keep.toml_path).fields[type_id]) == ["designer"]

    renamed = replace(pages, label="Made by", card=True)
    monkeypatch.setattr(tab, "ask_field", lambda current, type_id: renamed)
    with qtbot.waitSignal(config.changed, timeout=5000):
        tab.change_field()
    qtbot.waitUntil(lambda: tab.selected() == renamed, timeout=5000)
    assert session.own_fields[type_id]["designer"].label == "Made by"

    font = _id(session, FONT)
    session.tags.edit_field(font, renamed.key, "Sorkin")
    asked: list[int] = []

    def confirm(own: OwnField, count: int) -> bool:
        asked.append(count)
        return True

    monkeypatch.setattr(tab, "confirm_remove", confirm)
    with qtbot.waitSignal(config.changed, timeout=5000) as blocker:
        tab.remove_button.click()
    assert asked == [1]
    assert blocker.args == [
        "Removed the field 'Made by'; 1 item keeps its value as an Extra field. "
        "Edit → Undo brings it back."
    ]
    assert session.own_fields == {}
    assert tab.selected() is None


def test_a_refused_field_shows_why(
    qtbot: QtBot, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    type_id = _type_of(session, FONT)
    _, tab = _tab(qtbot, session)
    bad = OwnField(type_id, "x", "X")

    def refuse(own: OwnField) -> None:
        raise OwnFieldError("No")

    monkeypatch.setattr(session, "add_own_field", refuse)
    monkeypatch.setattr(tab, "ask_field", lambda current, type_id: bad)
    tab.add_field()
    qtbot.waitUntil(lambda: tab.error.text() == "No", timeout=5000)


def test_the_dialog_checks_as_you_type(qtbot: QtBot, session: KeepSession) -> None:
    type_id = _type_of(session, FONT)
    _, tab = _tab(qtbot, session)
    theme_label = session.theme_fields(type_id)[0][1]
    dialog = OwnFieldDialog(tab.types(), tab._check, type_id=type_id)
    qtbot.addWidget(dialog)
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()  # no label yet
    dialog.label_edit.setText("Pages read")
    assert ok.isEnabled()
    assert dialog.error.text() == ""
    assert dialog.field().name == "pages_read"
    assert dialog.field().type_id == type_id
    dialog.label_edit.setText(theme_label)
    assert not ok.isEnabled()
    assert "theme's fields" in dialog.error.text()

    dialog.label_edit.setText("Size")
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("integer"))
    assert dialog.display_combo.isEnabled()
    dialog.display_combo.setCurrentIndex(dialog.display_combo.findData("bytes"))
    assert dialog.field().display == "bytes"
    assert dialog.field().search == "range"
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("date"))
    assert not dialog.display_combo.isEnabled()
    assert dialog.field().display is None
    assert [dialog.search_combo.itemData(i) for i in range(dialog.search_combo.count())] == [
        "range",
        "none",
    ]


def test_changing_keeps_the_name_and_kind(qtbot: QtBot, session: KeepSession) -> None:
    type_id = _type_of(session, FONT)
    current = OwnField(type_id, "paid", "Paid", "number", display="bytes", column=True)
    session.add_own_field(current)
    _, tab = _tab(qtbot, session)
    dialog = OwnFieldDialog(tab.types(), tab._check, current, taken=tab._taken)
    qtbot.addWidget(dialog)
    assert not dialog.kind_combo.isEnabled()
    assert not dialog.type_combo.isEnabled()
    assert dialog.field() == current  # as it was: nothing to say
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog.label_edit.setText("Price")
    assert dialog.field() == replace(current, label="Price")
