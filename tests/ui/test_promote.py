"""Make it a field… and Turn back into an Extra field on an item's page (#582)."""

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDialogButtonBox, QToolButton, QWidget
from pytestqt.qtbot import QtBot

from tagalot.core.own_fields import OwnField
from tagalot.core.promote import KeyValue
from tagalot.core.session import KeepSession
from tagalot.ui.detail_view import MAKE_FIELD, TURN_BACK
from tagalot.ui.main_window import MainWindow
from tagalot.ui.own_fields import PromoteDialog, guess_kind, schema_types
from tests.ui.test_contents_search import _id, _open, session, window
from tests.ui.test_field_editing import _field
from tests.ui.test_own_fields import FONT, _type_of

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures


def _action(page: QWidget, button: str, text: str) -> QAction:
    remove = page.findChild(QToolButton, button)
    assert isinstance(remove, QToolButton), button
    menu = remove.menu()
    assert menu is not None
    return next(a for a in menu.actions() if a.text() == text)


def test_making_an_extra_field_a_field_and_turning_it_back(
    qtbot: QtBot, window: MainWindow, session: KeepSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    type_id = _type_of(session, FONT)
    font = _id(session, FONT)
    session.tags.edit_extra(font, "weight", "400", new=True)
    page = _open(qtbot, window, session, FONT)
    qtbot.waitUntil(lambda: page.details.findChild(QToolButton, "remove_weight") is not None)

    asked: list[tuple[str, list[KeyValue], str]] = []
    weight = OwnField(type_id, "weight", "Weight", "integer")

    def ask(key: str, values: list[KeyValue], tid: str) -> list[OwnField]:
        asked.append((key, values, tid))
        return [weight]

    monkeypatch.setattr(window, "ask_promotion", ask)
    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        _action(page.details, "remove_weight", MAKE_FIELD).trigger()
    assert [(k, [v.text for v in vs], t) for k, vs, t in asked] == [("weight", ["400"], type_id)]
    assert blocker.args == ["Make 'weight' the field 'Weight' (1 item). Edit → Undo reverses it."]
    qtbot.waitUntil(lambda: page.details.findChild(QToolButton, "more_own:weight") is not None)
    assert _field(page, "own:weight").label.text() == "400"
    assert window.undo_action.text().startswith("Undo Make 'weight'")

    with qtbot.waitSignal(window.tag_actions.changed, timeout=5000) as blocker:
        _action(page.details, "more_own:weight", TURN_BACK).trigger()
    assert blocker.args == [
        "Turn 'Weight' back into an Extra field (1 item). Edit → Undo reverses it."
    ]
    qtbot.waitUntil(lambda: page.details.findChild(QToolButton, "remove_Weight") is not None)
    assert session.own_fields == {}


def test_the_promote_dialog_guesses_and_previews(qtbot: QtBot, session: KeepSession) -> None:
    type_id = _type_of(session, FONT)
    other = next(t for t, _ in schema_types(session) if t != type_id)
    values = [
        KeyValue(1, type_id, "A", "copies", "12"),
        KeyValue(2, type_id, "B", "copies", "lots"),
        KeyValue(3, other, "C", "Copies", "30"),
    ]
    assert guess_kind(values[:1]) == "integer"
    assert guess_kind(values) == "text"
    dialog = PromoteDialog(
        schema_types(session), session.own_field_problem, "copies", values, type_id
    )
    qtbot.addWidget(dialog)
    assert dialog.label_edit.text() == "Copies"
    assert dialog.kind_combo.currentData() == "text"  # "lots" isn't a number
    assert dialog.chosen_types() == [type_id]  # the item's own; the other is offered
    assert len(dialog.type_boxes) == 2
    assert dialog.preview.text() == "2 items: all move into the field."
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("integer"))
    assert dialog.preview.text() == (
        "2 items: 1 read as whole numbers and move; 1 don't, and stay Extra fields "
        "(below), for you to fix."
    )
    assert [dialog.misfits.item(i).text() for i in range(dialog.misfits.count())] == ["B: lots"]
    dialog.type_boxes[other].setChecked(True)
    assert dialog.preview.text().startswith("3 items: 2 read as whole numbers")
    fields = dialog.fields()
    assert [(f.type_id, f.name, f.label, f.kind) for f in fields] == [
        (type_id, "copies", "Copies", "integer"),
        (other, "copies", "Copies", "integer"),
    ]
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert dialog.error.text() == ""
    assert ok.isEnabled()
    for box in dialog.type_boxes.values():
        box.setChecked(False)
    assert not ok.isEnabled()
    assert dialog.error.text() == "Choose a type for the field"
