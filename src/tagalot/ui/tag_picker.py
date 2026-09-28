"""Dialogs for the tag manager's operations (DESIGN.md §7).

- :class:`TagPickerDialog`: choose a tag from a filterable tree, with some tags greyed out.
  :func:`move_dialog` ("Move to…", with a "Top level" choice) and :func:`merge_dialog`
  ("Merge into…") are its two uses.
- :class:`DeleteDialog`: confirm a delete; for a tag with sub-tags, choose between deleting
  them too and moving them up. Both show how many items lose a tag.

Counts come from the tag manager's usage figures, so the dialogs open without waiting.
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

from tagalot.core.tags import PATH_SEPARATOR, DeleteMode, TagTree, TagUsage
from tagalot.ui.models.tag_tree import TagTreeModel

AnyIndex = QModelIndex | QPersistentModelIndex


def items_text(count: int) -> str:
    return "1 item" if count == 1 else f"{count:,} items"


class _PickerModel(TagTreeModel):
    """The tag tree with ``blocked`` tags disabled (shown greyed out)."""

    def __init__(self, blocked: frozenset[int], reason: str) -> None:
        super().__init__()
        self.blocked = blocked
        self.reason = reason

    def flags(self, index: AnyIndex) -> Qt.ItemFlag:
        tag_id = self.tag_id(index)
        if tag_id is None or tag_id in self.blocked:
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index: AnyIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.ToolTipRole and self.tag_id(index) in self.blocked:
            return self.reason
        return super().data(index, role)


class TagPickerDialog(QDialog):
    """Pick a tag for ``tag_id``'s operation. After ``exec()``, :meth:`target` is the chosen
    tag (``None`` for the top level, when it is offered)."""

    def __init__(
        self,
        tree: TagTree,
        tag_id: int,
        *,
        title: str,
        prompt: str,
        ok_text: str,
        blocked_reason: str,
        top_level: bool,
        unchanged: int | None = -1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(420, 480)
        self.tag_id = tag_id
        self.unchanged = unchanged
        """A choice that would change nothing (the tag's current parent, for a move)."""

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter tags…")
        self.filter_edit.setClearButtonEnabled(True)
        self.model = _PickerModel(tree.descendants(tag_id), blocked_reason)
        self.model.set_tree(tree)
        self.view = QTreeView()
        self.view.setModel(self.model)
        self.view.setHeaderHidden(True)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.expandAll()
        self.top_level = QRadioButton("Top level (no parent)")
        self.top_level.setAutoExclusive(False)
        self.top_level.setVisible(top_level)
        self.explanation = QLabel()
        self.explanation.setWordWrap(True)
        self.message = QLabel()
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText(ok_text)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        prompt_label = QLabel(prompt)
        prompt_label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(prompt_label)
        layout.addWidget(self.top_level)
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.explanation)
        layout.addWidget(self.message)
        layout.addWidget(self.buttons)

        self.filter_edit.textChanged.connect(self._filter)
        self.view.selectionModel().currentChanged.connect(lambda *_: self._chose_tag())
        self.top_level.toggled.connect(self._chose_top)
        self._update()

    def target(self) -> int | None:
        """The chosen tag, or ``None`` for the top level."""
        if self.top_level.isChecked():
            return None
        return self.model.tag_id(self.view.currentIndex())

    def chosen(self) -> bool:
        return self.top_level.isChecked() or self.view.currentIndex().isValid()

    def choose(self, tag_id: int | None) -> None:
        """Choose a target (``None``: the top level), as the user would."""
        if tag_id is None:
            self.top_level.setChecked(True)
        else:
            self.view.setCurrentIndex(self.model.index_of(tag_id))

    def describe(self, target: int | None) -> str:
        """What choosing ``target`` will do (shown under the tree). Subclasses may say."""
        return ""

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
        chosen = self.chosen()
        target = self.target() if chosen else -1
        if not chosen:
            self.message.setText("Choose a tag.")
        elif target == self.unchanged:
            self.message.setText("It's already there.")
        else:
            self.message.setText("")
        self.explanation.setText(self.describe(target) if chosen else "")
        self.explanation.setVisible(bool(self.explanation.text()))
        self.ok_button.setEnabled(chosen and target != self.unchanged)


def move_dialog(tree: TagTree, tag_id: int, parent: QWidget | None = None) -> TagPickerDialog:
    """ "Move to…": a new parent for ``tag_id``, or the top level."""
    name = tree.node(tag_id).name
    return TagPickerDialog(
        tree,
        tag_id,
        title=f"Move “{name}”",
        prompt=f"Move “{name}” (with its sub-tags) under:",
        ok_text="Move",
        blocked_reason="A tag can't move under itself or one of its own sub-tags.",
        top_level=True,
        unchanged=tree.node(tag_id).parent_id,
        parent=parent,
    )


class MergeDialog(TagPickerDialog):
    """ "Merge into…": the tag ``tag_id`` is folded into (§7)."""

    def __init__(
        self,
        tree: TagTree,
        tag_id: int,
        usage: TagUsage | None,
        parent: QWidget | None = None,
    ) -> None:
        self.tree = tree
        self.usage = usage
        name = tree.node(tag_id).name
        super().__init__(
            tree,
            tag_id,
            title=f"Merge “{name}”",
            prompt=f"Merge “{name}” into:",
            ok_text="Merge",
            blocked_reason="A tag can't merge into itself or one of its own sub-tags.",
            top_level=False,
            parent=parent,
        )

    def describe(self, target: int | None) -> str:
        if target is None or target not in self.tree:
            return ""
        source = self.tree.node(self.tag_id)
        into = PATH_SEPARATOR.join(self.tree.path(target))
        parts = []
        if self.usage is not None:
            parts.append(
                f"{items_text(self.usage.direct)} tagged “{source.name}” will be "
                f"tagged “{into}” instead."
            )
        if self.tree.children(self.tag_id):
            parts.append(f"Its sub-tags move under “{into}”.")
        parts.append(f"“{source.name}” becomes another name for it, and is then deleted.")
        return " ".join(parts)


def merge_dialog(
    tree: TagTree, tag_id: int, usage: TagUsage | None, parent: QWidget | None = None
) -> MergeDialog:
    return MergeDialog(tree, tag_id, usage, parent)


class DeleteDialog(QDialog):
    """Confirm deleting ``tag_id``. With sub-tags, choose what happens to them; either way
    it says how many items lose a tag. :meth:`mode` gives the choice (``None`` for a tag
    without sub-tags)."""

    def __init__(
        self,
        tree: TagTree,
        tag_id: int,
        usage: TagUsage | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        node = tree.node(tag_id)
        self.setWindowTitle(f"Delete “{node.name}”")
        self.usage = usage
        self.has_children = bool(tree.children(tag_id))
        subtags = len(tree.descendants(tag_id)) - 1
        up_to = (
            f"“{tree.node(node.parent_id).name}”" if node.parent_id is not None else "the top level"
        )
        self.subtree = QRadioButton(
            f"Delete “{node.name}” and its {subtags} sub-tag{'s' * (subtags != 1)}"
        )
        self.promote = QRadioButton(f"Delete only “{node.name}”; move its sub-tags up to {up_to}")
        self.subtree.setChecked(True)
        self.subtree.setVisible(self.has_children)
        self.promote.setVisible(self.has_children)
        self.effect = QLabel()
        self.effect.setWordWrap(True)
        note = QLabel("Items are never deleted, only their tags. Edit → Undo brings it back.")
        note.setWordWrap(True)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Delete")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        question = QLabel(f"Delete the tag “{PATH_SEPARATOR.join(tree.path(tag_id))}”?")
        question.setWordWrap(True)
        layout.addWidget(question)
        layout.addWidget(self.subtree)
        layout.addWidget(self.promote)
        layout.addWidget(self.effect)
        layout.addWidget(note)
        layout.addWidget(self.buttons)
        self.subtree.toggled.connect(self._update)
        self.promote.toggled.connect(self._update)
        self._update()

    def mode(self) -> DeleteMode | None:
        if not self.has_children:
            return None
        return DeleteMode.SUBTREE if self.subtree.isChecked() else DeleteMode.PROMOTE

    def losing(self) -> int | None:
        """How many items lose a tag with the current choice (``None`` if unknown)."""
        if self.usage is None:
            return None
        return self.usage.with_subtags if self.mode() is DeleteMode.SUBTREE else self.usage.direct

    def _update(self) -> None:
        losing = self.losing()
        if losing is None:
            self.effect.setText("")
        elif losing:
            self.effect.setText(f"{items_text(losing)} will lose a tag.")
        else:
            self.effect.setText("No items use it.")
