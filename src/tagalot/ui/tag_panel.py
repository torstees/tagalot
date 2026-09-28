"""The tagging panel: the tag tree with a filter box (DESIGN.md §12 "Tagging panel").

Typing in the filter box narrows the tree to matching tags (names and aliases) and their
ancestors, expanded so every match shows; clearing it restores the folds the user had.
Down (or Enter) in the filter box moves to the first match. The tag tree is loaded in a
worker; call :meth:`TagPanel.reload` after tags change.
"""

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QThreadPool, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QAbstractItemView, QLabel, QLineEdit, QTreeView, QVBoxLayout, QWidget

from tagalot.core.session import KeepSession
from tagalot.core.tags import TagTree
from tagalot.ui.models.tag_tree import TagTreeModel
from tagalot.ui.workers import run_in_pool


class TagPanel(QWidget):
    """Filter box and tag tree. Emits :attr:`current_changed` with the highlighted tag id
    (or ``None``)."""

    current_changed = Signal(object)

    def __init__(
        self,
        session: KeepSession,
        *,
        pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self._pool = pool
        self._expanded: set[int] = set()  # the user's folds, kept while filtering
        self._restoring = False
        self.loaded = False

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter tags…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setToolTip("Type part of a tag name or alias")
        self.filter_edit.textChanged.connect(self._filter_changed)
        self.filter_edit.installEventFilter(self)

        self.model = TagTreeModel(self)
        self.view = QTreeView()
        self.view.setModel(self.model)
        self.view.setHeaderHidden(True)
        self.view.setUniformRowHeights(True)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Several tags can be selected and dragged onto items together.
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setDragEnabled(True)
        self.view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.view.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.view.expanded.connect(lambda index: self._folded(index, True))
        self.view.collapsed.connect(lambda index: self._folded(index, False))
        self.view.selectionModel().currentChanged.connect(
            lambda current, _previous: self.current_changed.emit(self.model.tag_id(current))
        )

        self.message = QLabel("Loading tags…")
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.message)
        self.reload()

    # --- loading ---

    def reload(self) -> None:
        """Load the current tag tree in a worker, keeping folds, filter, and highlight."""

        def loaded(tree: TagTree) -> None:
            if shiboken6.isValid(self):  # the panel may have closed meanwhile
                self.set_tree(tree)

        run_in_pool(self.session.tag_cache.get, on_done=loaded, pool=self._pool)

    def set_tree(self, tree: TagTree) -> None:
        first_load = not self.loaded
        self.loaded = True
        current = self.current_tag()
        self._expanded &= set(_all_ids(tree))  # forget deleted tags
        if first_load:
            self._expanded = set(tree.children(None))  # start with the top level open
        self.model.set_tree(tree)
        self._after_reset()
        if current is not None:
            self.select_tag(current)

    # --- state ---

    def current_tag(self) -> int | None:
        return self.model.tag_id(self.view.currentIndex())

    def select_tag(self, tag_id: int) -> bool:
        """Highlight a shown tag; returns whether it is shown."""
        index = self.model.index_of(tag_id)
        if not index.isValid():
            return False
        self.view.setCurrentIndex(index)
        self.view.scrollTo(index)
        return True

    def expanded_tags(self) -> set[int]:
        return set(self._expanded)

    # --- filtering ---

    def _filter_changed(self, text: str) -> None:
        current = self.current_tag()
        self.model.set_filter(text)
        self._after_reset()
        if current is not None and not self.select_tag(current) and self.model.filtering:
            first = self.model.first_match()
            if first.isValid():
                self.view.setCurrentIndex(first)

    def _after_reset(self) -> None:
        """Re-apply folds after the model reset: everything open while filtering, else the
        user's own folds."""
        self._restoring = True
        try:
            if self.model.filtering:
                self.view.expandAll()
            else:
                for tag_id in self._expanded:
                    index = self.model.index_of(tag_id)
                    if index.isValid():
                        self.view.expand(index)
        finally:
            self._restoring = False
        self._show_message()

    def _folded(self, index: object, expanded: bool) -> None:
        if self._restoring or self.model.filtering:
            return  # folds made while filtering don't replace the user's own
        tag_id = self.model.tag_id(index)  # type: ignore[arg-type]
        if tag_id is None:
            return
        if expanded:
            self._expanded.add(tag_id)
        else:
            self._expanded.discard(tag_id)

    def _show_message(self) -> None:
        tree = self.model.tree
        if tree is None:
            text = "Loading tags…"
        elif len(tree) == 0:
            text = "No tags yet."
        elif self.model.filtering and not self.model.matches():
            text = f"No tags match “{self.filter_edit.text().strip()}”."
        else:
            text = ""
        self.message.setText(text)
        self.message.setVisible(bool(text))

    # --- keyboard ---

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.filter_edit and event.type() == QEvent.Type.KeyPress:
            assert isinstance(event, QKeyEvent)
            if event.key() == Qt.Key.Key_Down:
                self._focus_tree()
                return True
            if event.key() == Qt.Key.Key_Escape and self.filter_edit.text():
                self.filter_edit.clear()
                return True
        return super().eventFilter(watched, event)

    def _focus_tree(self) -> None:
        """Move from the filter box into the tree, on the first match (or the first tag)."""
        if not self.view.currentIndex().isValid():
            first = self.model.first_match() if self.model.filtering else self.model.index(0, 0)
            if first.isValid():
                self.view.setCurrentIndex(first)
        self.view.setFocus(Qt.FocusReason.OtherFocusReason)


def _all_ids(tree: TagTree) -> list[int]:
    ids: list[int] = []
    stack = list(tree.children(None))
    while stack:
        tag_id = stack.pop()
        ids.append(tag_id)
        stack.extend(tree.children(tag_id))
    return ids
