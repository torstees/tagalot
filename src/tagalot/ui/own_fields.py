"""Keep configuration's Fields tab: the keep's own fields, by type (#581).

Each type lists the fields the user added to it, beside its theme's, to add more, change
how one is labelled, searched, or shown, or remove one (its values become Extra fields).
A field's name and kind stay as made, since its values are stored by them.
"""

import logging
from collections.abc import Callable

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
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.own_fields import KINDS, OwnField, name_for, searches_for
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
        form = QFormLayout()
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
        return [(t.type_id, entity_label(t.entity)) for t in self.session.schema.entities.values()]

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
            items = "1 item" if count == 1 else f"{count} items"
            self._run(
                lambda: session.remove_own_field(own),
                f"Removed the field {own.label!r}"
                + (f"; {items} keep its value as an Extra field." if count else "."),
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
            f"Remove the field {own.label!r}? {what} This can't be undone.",
        )
        return answer == QMessageBox.StandardButton.Yes
