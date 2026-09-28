"""The "Move to…" picker: choose a new parent for a tag (DESIGN.md §7).

A filterable tag tree plus a "Top level" choice. The tag being moved and everything under it
are shown greyed out, since a tag can't move under itself; OK is enabled only for a
valid target.
"""

from typing import Any

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QRadioButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.tags import TagTree
from tagalot.ui.models.tag_tree import TagTreeModel

AnyIndex = QModelIndex | QPersistentModelIndex


class _PickerModel(TagTreeModel):
    """The tag tree with ``blocked`` tags (the one moving and its sub-tags) disabled."""

    def __init__(self, blocked: frozenset[int]) -> None:
        super().__init__()
        self.blocked = blocked

    def flags(self, index: AnyIndex) -> Qt.ItemFlag:
        tag_id = self.tag_id(index)
        if tag_id is None:
            return Qt.ItemFlag.NoItemFlags
        if tag_id in self.blocked:
            return Qt.ItemFlag.NoItemFlags  # shown greyed out, can't be chosen
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index: AnyIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.ToolTipRole and self.tag_id(index) in self.blocked:
            return "A tag can't move under itself or one of its own sub-tags."
        return super().data(index, role)


class MoveToDialog(QDialog):
    """Pick where ``tag_id`` should go. After ``exec()``, :meth:`target` is the new parent
    (``None`` for the top level)."""

    def __init__(self, tree: TagTree, tag_id: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        name = tree.node(tag_id).name
        self.setWindowTitle(f"Move “{name}”")
        self.resize(420, 480)
        self.tag_id = tag_id
        self.current_parent = tree.node(tag_id).parent_id

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter tags…")
        self.filter_edit.setClearButtonEnabled(True)
        self.model = _PickerModel(tree.descendants(tag_id))
        self.model.set_tree(tree)
        self.view = QTreeView()
        self.view.setModel(self.model)
        self.view.setHeaderHidden(True)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.expandAll()
        self.top_level = QRadioButton("Top level (no parent)")
        self.top_level.setAutoExclusive(False)
        self.message = QLabel()
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Move")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Move “{name}” (with its sub-tags) under:"))
        layout.addWidget(self.top_level)
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.message)
        layout.addWidget(self.buttons)

        self.filter_edit.textChanged.connect(self._filter)
        self.view.selectionModel().currentChanged.connect(lambda *_: self._chose_tag())
        self.top_level.toggled.connect(self._chose_top)
        self._update()

    def target(self) -> int | None:
        """The chosen new parent, or ``None`` for the top level."""
        if self.top_level.isChecked():
            return None
        return self.model.tag_id(self.view.currentIndex())

    def choose(self, tag_id: int | None) -> None:
        """Choose a target (``None``: the top level), as the user would."""
        if tag_id is None:
            self.top_level.setChecked(True)
        else:
            self.view.setCurrentIndex(self.model.index_of(tag_id))

    def _filter(self, text: str) -> None:
        self.model.set_filter(text)
        self.view.expandAll()
        self._update()

    def _chose_tag(self) -> None:
        if self.view.currentIndex().isValid():
            self.top_level.setChecked(False)
        self._update()

    def _chose_top(self, checked: bool) -> None:
        if checked:
            self.view.clearSelection()
            self.view.setCurrentIndex(QModelIndex())
        self._update()

    def _update(self) -> None:
        chosen = self.top_level.isChecked() or self.view.currentIndex().isValid()
        target = self.target() if chosen else -1
        ok = chosen and target != self.current_parent
        if not chosen:
            self.message.setText("Choose where to move it.")
        elif target == self.current_parent:
            self.message.setText("It's already there.")
        else:
            self.message.setText("")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)
