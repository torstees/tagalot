"""Keep configuration's Fields tab: the keep's own fields, by type (#581).

Each type lists the fields the user added to it, beside its theme's, to add more, change
how one is labelled, searched, or shown, or remove one (its values become Extra fields).
A field's name and kind stay as made, since its values are stored by them.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import replace

import shiboken6
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.own_fields import KINDS, OwnField, name_for, searches_for
from tagalot.core.promote import KeyValue, fits, preview, type_counts
from tagalot.core.session import KeepSession
from tagalot.themes.api import entity_label
from tagalot.ui.colors import ERROR, css
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

SEARCH_LABELS = {
    "words": "By its words",
    "range": "By a range",
    "choice": "By picking values",
    "none": "Not searched",
}
DISPLAY_LABELS = {
    None: "As a number",
    "bytes": "As a size (3.0 MB)",
    "duration": "As a time (3:25)",
}
_FIELD = Qt.ItemDataRole.UserRole + 1


class OwnFieldDialog(QDialog):
    """Add one of the keep's own fields, or change one (``current``): its type, label,
    kind, how it is searched and shown. ``check`` says what is wrong with a field, or
    None; OK stays until it has nothing to say."""

    def __init__(
        self,
        types: list[tuple[str, str]],
        check: Callable[[OwnField], str | None],
        current: OwnField | None = None,
        type_id: str | None = None,
        taken: Callable[[str], set[str]] = lambda _: set(),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.current = current
        self._check = check
        self._taken = taken
        self.setWindowTitle("Change field" if current else "Add a field")
        self.type_combo = QComboBox()
        for tid, label in types:
            self.type_combo.addItem(label, tid)
        wanted = current.type_id if current else type_id
        if wanted is not None and (at := self.type_combo.findData(wanted)) >= 0:
            self.type_combo.setCurrentIndex(at)
        self.type_combo.setEnabled(current is None)
        self.label_edit = QLineEdit(current.label if current else "")
        self.label_edit.setPlaceholderText("Pages read")
        self.kind_combo = QComboBox()
        for kind in KINDS:
            self.kind_combo.addItem(kind.capitalize(), kind)
        if current is not None:
            self.kind_combo.setCurrentIndex(self.kind_combo.findData(current.kind))
            self.kind_combo.setEnabled(False)
            self.kind_combo.setToolTip("A field's kind stays as made: its values are kept by it")
        self.search_combo = QComboBox()
        self.display_combo = QComboBox()
        for display, text in DISPLAY_LABELS.items():
            self.display_combo.addItem(text, display)
        self.card_box = QCheckBox("Show it on grid cards")
        self.column_box = QCheckBox("Show it as a column in lists")
        self.error = QLabel()
        self.error.setObjectName("error")
        self.error.setStyleSheet(f"color: {css(ERROR)};")
        self.error.setWordWrap(True)
        form = self.form = QFormLayout()
        form.addRow("Type:", self.type_combo)
        form.addRow("Label:", self.label_edit)
        form.addRow("Kind:", self.kind_combo)
        form.addRow("Searched:", self.search_combo)
        form.addRow("Shown:", self.display_combo)
        form.addRow("", self.card_box)
        form.addRow("", self.column_box)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(self.buttons)

        self._fill_kind()
        if current is not None:
            self.search_combo.setCurrentIndex(self.search_combo.findData(current.search))
            self.display_combo.setCurrentIndex(self.display_combo.findData(current.display))
            self.card_box.setChecked(current.card)
            self.column_box.setChecked(current.column)
        self.kind_combo.currentIndexChanged.connect(lambda _: self._fill_kind())
        for signal in (
            self.label_edit.textChanged,
            self.type_combo.currentIndexChanged,
            self.search_combo.currentIndexChanged,
            self.display_combo.currentIndexChanged,
        ):
            signal.connect(lambda *_: self._show_problem())
        self._show_problem()

    def _fill_kind(self) -> None:
        kind = self.kind_combo.currentData()
        self.search_combo.clear()
        for search in searches_for(kind):
            self.search_combo.addItem(SEARCH_LABELS[search], search)
        number = kind in ("integer", "number")
        self.display_combo.setEnabled(number)
        if not number:
            self.display_combo.setCurrentIndex(0)

    def field(self) -> OwnField:
        """The field as the dialog has it now."""
        label = self.label_edit.text().strip()
        type_id = str(self.type_combo.currentData())
        name = self.current.name if self.current else name_for(label, self._taken(type_id))
        return OwnField(
            type_id,
            name,
            label,
            str(self.kind_combo.currentData()),
            str(self.search_combo.currentData() or ""),
            self.display_combo.currentData() if self.display_combo.isEnabled() else None,
            self.card_box.isChecked(),
            self.column_box.isChecked(),
        )

    def _show_problem(self) -> None:
        problem = None if not self.label_edit.text().strip() else self._check(self.field())
        self.error.setText(problem or "")
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(bool(self.label_edit.text().strip()) and problem is None)


def schema_types(session: KeepSession) -> list[tuple[str, str]]:
    """The keep's theme's types as ``(type id, label)``, in its order."""
    return [(t.type_id, entity_label(t.entity)) for t in session.schema.entities.values()]


GUESSES = ("integer", "number", "date", "yes/no", "link")
"""The kinds :class:`PromoteDialog` tries, in order, before text: the first every value
reads as."""
MISFITS_SHOWN = 200


def guess_kind(values: Iterable[KeyValue]) -> str:
    """The kind an Extra field's values all read as (text when there's none)."""
    texts = [v.text for v in values]
    return next((k for k in GUESSES if texts and all(fits(k, t) for t in texts)), "text")


class PromoteDialog(OwnFieldDialog):
    """**Make it a field…** (#582): the Add-a-field dialog for an Extra field's key, with
    the types where the key appears (the item's own ticked) and a preview of how many
    values read as the kind; those that don't are listed, and stay Extra fields."""

    def __init__(
        self,
        types: list[tuple[str, str]],
        check: Callable[[OwnField], str | None],
        key: str,
        values: list[KeyValue],
        type_id: str,
        taken: Callable[[str], set[str]] = lambda _: set(),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(types, check, None, type_id, taken, parent)
        self.key = key
        self.values = values
        self.setWindowTitle(f"Make {key!r} a field")
        counts = type_counts(values)
        labels = dict(types)
        self.form.setRowVisible(self.type_combo, False)
        self.type_boxes: dict[str, QCheckBox] = {}
        boxes = QVBoxLayout()
        for tid, count in sorted(counts.items(), key=lambda t: (t[0] != type_id, t[0])):
            box = QCheckBox(f"{labels.get(tid, tid)} ({_items(count)})")
            box.setChecked(tid == type_id)
            box.toggled.connect(lambda _on: self._show_problem())
            self.type_boxes[tid] = box
            boxes.addWidget(box)
        self.form.insertRow(0, "For:", boxes)
        self.preview = QLabel()
        self.preview.setObjectName("preview")
        self.preview.setWordWrap(True)
        self.misfits = QListWidget()
        self.misfits.setObjectName("misfits")
        self.misfits.setMaximumHeight(140)
        layout = self.layout()
        assert isinstance(layout, QVBoxLayout)
        layout.insertWidget(1, self.preview)
        layout.insertWidget(2, self.misfits)
        label = key.strip()
        self.label_edit.setText(label[:1].upper() + label[1:])  # pages -> Pages
        self.kind_combo.setCurrentIndex(self.kind_combo.findData(guess_kind(values)))
        self._show_problem()

    def chosen_types(self) -> list[str]:
        return [t for t, box in getattr(self, "type_boxes", {}).items() if box.isChecked()]

    def fields(self) -> list[OwnField]:
        """The field for each chosen type: the same name, label, and kind."""
        label = self.label_edit.text().strip()
        types = self.chosen_types()
        taken = set().union(*(self._taken(t) for t in types)) if types else set()
        one = self.field()
        name = name_for(label, taken)
        return [replace(one, type_id=t, name=name) for t in types]

    def _show_problem(self) -> None:
        if not hasattr(self, "type_boxes"):
            return  # still being made
        fields = self.fields()
        problem = None
        if not fields:
            problem = "Choose a type for the field"
        elif self.label_edit.text().strip():
            problem = next((p for f in fields if (p := self._check(f)) is not None), None)
        self.error.setText(problem or "")
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(bool(self.label_edit.text().strip()) and problem is None)
        self._show_preview()

    def _show_preview(self) -> None:
        kind = str(self.kind_combo.currentData())
        found = preview(self.values, kind, self.chosen_types())
        what = {"integer": "whole numbers", "number": "numbers", "date": "dates"}.get(
            kind, "yes or no" if kind == "yes/no" else f"{kind}s"
        )
        text = f"{_items(found.items)}: "
        if not found.misfits:
            text += "all move into the field."
        else:
            text += f"{found.fit} read as {what} and move; {len(found.misfits)} don't, and "
            text += "stay Extra fields (below), for you to fix."
        self.preview.setText(text)
        self.misfits.clear()
        for value in found.misfits[:MISFITS_SHOWN]:
            self.misfits.addItem(f"{value.title}: {value.text}")
        if len(found.misfits) > MISFITS_SHOWN:
            self.misfits.addItem(f"and {len(found.misfits) - MISFITS_SHOWN} more")
        self.misfits.setVisible(bool(found.misfits))


def _items(count: int) -> str:
    return "1 item" if count == 1 else f"{count} items"


class OwnFieldsTab(QWidget):
    """The Fields tab; ``saved`` takes a status-bar message after a change was saved."""

    def __init__(
        self,
        session: KeepSession,
        saved: Callable[[str], None],
        pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self._saved = saved
        self._pool = pool
        intro = QLabel(
            "Fields of your own for a type, beside its theme's: edit them on an item's page, "
            "where they show under Details. They're kept in keep.toml, so they travel with "
            "the keep."
        )
        intro.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setObjectName("own_fields")
        self.tree.setHeaderLabels(["Field", "Kind", "Searched", "Shown"])
        self.tree.setRootIsDecorated(False)
        header = self.tree.header()
        for column in range(3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.tree.currentItemChanged.connect(lambda *_: self._enable())
        self.tree.itemDoubleClicked.connect(lambda *_: self.change_field())
        self.add_button = QPushButton("Add field…")
        self.add_button.clicked.connect(self.add_field)
        self.change_button = QPushButton("Change…")
        self.change_button.clicked.connect(self.change_field)
        self.remove_button = QPushButton("Remove…")
        self.remove_button.clicked.connect(self.remove_field)
        buttons = QHBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.change_button)
        buttons.addStretch(1)
        buttons.addWidget(self.remove_button)
        self.error = QLabel()
        self.error.setObjectName("error")
        self.error.setStyleSheet(f"color: {css(ERROR)};")
        self.error.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.error)
        layout.addLayout(buttons)
        self.reload()

    def types(self) -> list[tuple[str, str]]:
        """The theme's types as ``(type id, label)``, in its order."""
        return schema_types(self.session)

    def reload(self) -> None:
        """Show the keep's own fields as ``keep.toml`` has them now."""
        current = self.selected()
        self.tree.clear()
        labels = dict(self.types())
        for type_id, fields in self.session.own_fields.items():
            if not fields:
                continue
            heading = QTreeWidgetItem([labels.get(type_id, type_id)])
            font = heading.font(0)
            font.setBold(True)
            heading.setFont(0, font)
            heading.setFlags(Qt.ItemFlag.ItemIsEnabled)
            heading.setFirstColumnSpanned(True)
            self.tree.addTopLevelItem(heading)
            for own in fields.values():
                shown = ", ".join(w for w, on in (("cards", own.card), ("lists", own.column)) if on)
                item = QTreeWidgetItem(
                    [
                        f"   {own.label}",
                        own.kind.capitalize(),
                        SEARCH_LABELS[own.search],
                        shown.capitalize() or "Its page",
                    ]
                )
                item.setData(0, _FIELD, own)
                clash = self.session.theme_clash(own)
                if clash is not None:  # a later theme version added one like it
                    item.setText(0, f"   {own.label}  ⚠")
                    item.setToolTip(
                        0,
                        f"The theme now has a field {clash!r} too. Both keep their values; "
                        "rename this one's label to tell them apart.",
                    )
                self.tree.addTopLevelItem(item)
                if current is not None and (own.type_id, own.name) == (
                    current.type_id,
                    current.name,
                ):
                    self.tree.setCurrentItem(item)
        if self.tree.topLevelItemCount() == 0:
            empty = QTreeWidgetItem(["No fields of your own yet. Add one to begin."])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            empty.setFirstColumnSpanned(True)
            self.tree.addTopLevelItem(empty)
        self._enable()

    def selected(self) -> OwnField | None:
        item = self.tree.currentItem()
        own = item.data(0, _FIELD) if item is not None else None
        return own if isinstance(own, OwnField) else None

    def select(self, type_id: str, name: str) -> None:
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            if item is None:
                continue
            own = item.data(0, _FIELD)
            if isinstance(own, OwnField) and (own.type_id, own.name) == (type_id, name):
                self.tree.setCurrentItem(item)

    def _enable(self) -> None:
        on = self.selected() is not None
        self.change_button.setEnabled(on)
        self.remove_button.setEnabled(on)

    def _taken(self, type_id: str) -> set[str]:
        return set(self.session.own_fields.get(type_id, {}))

    def _check(self, own: OwnField) -> str | None:
        if own.name in self._taken(own.type_id):  # a change
            return self.session.own_field_change_problem(own)
        return self.session.own_field_problem(own)

    # --- actions ---

    def add_field(self) -> None:
        current = self.selected()
        own = self.ask_field(None, current.type_id if current else None)
        if own is None:
            return
        session = self.session
        self._run(
            lambda: session.add_own_field(own),
            f"Added the field {own.label!r}; set it on an item's page.",
            lambda: self.select(own.type_id, own.name),
        )

    def change_field(self) -> None:
        current = self.selected()
        if current is None:
            return
        own = self.ask_field(current, current.type_id)
        if own is None or own == current:
            return
        session = self.session
        self._run(
            lambda: session.change_own_field(own),
            f"Changed the field {own.label!r}.",
            lambda: self.select(own.type_id, own.name),
        )

    def remove_field(self) -> None:
        own = self.selected()
        if own is None:
            return
        session = self.session

        def counted(count: int) -> None:
            if not shiboken6.isValid(self) or not self.confirm_remove(own, count):
                return
            verb = "keeps" if count == 1 else "keep"
            kept = f"; {_items(count)} {verb} its value as an Extra field" if count else ""
            self._run(
                lambda: session.remove_own_field(own),
                f"Removed the field {own.label!r}{kept}. Edit \u2192 Undo brings it back.",
            )

        run_in_pool(lambda: session.count_own_values(own), on_done=counted, pool=self._pool)

    def _run(
        self, work: Callable[[], object], message: str, then: Callable[[], None] | None = None
    ) -> None:
        self.error.clear()

        def done(_: object) -> None:
            if not shiboken6.isValid(self):
                return
            self.reload()
            self._saved(message)
            if then is not None:
                then()

        def failed(error: BaseException) -> None:
            if not shiboken6.isValid(self):
                return
            logger.error("Changing the keep's own fields failed", exc_info=error)
            self.error.setText(str(error))

        run_in_pool(work, on_done=done, on_error=failed, pool=self._pool)

    # --- asking (tests replace these) ---

    def ask_field(self, current: OwnField | None, type_id: str | None) -> OwnField | None:
        """The field the user describes (a new one, or ``current`` changed), or None."""
        dialog = OwnFieldDialog(
            self.types(), self._check, current, type_id, self._taken, parent=self
        )
        return dialog.field() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def confirm_remove(self, own: OwnField, count: int) -> bool:
        if count:
            items = "1 item has" if count == 1 else f"{count} items have"
            what = (
                f"{items} a value of it. Each keeps its value as an Extra field called "
                f"{own.label!r}."
            )
        else:
            what = "No item has a value of it."
        answer = QMessageBox.question(
            self,
            "Remove field",
            f"Remove the field {own.label!r}? {what} Edit \u2192 Undo brings it back.",
        )
        return answer == QMessageBox.StandardButton.Yes
