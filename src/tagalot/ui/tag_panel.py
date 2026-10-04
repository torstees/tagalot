"""The tagging panel: the tag tree with a filter box (DESIGN.md §12 "Tagging panel").

Typing in the filter box narrows the tree to matching tags (names, aliases, descriptions)
and their ancestors, expanded so every match shows; clearing it restores the folds the user
had. Down moves into the tree, on the first match.

Keyboard tagging: **Enter** asks to apply the highlighted tag (or the tags selected in the
tree) to the selected items, **Shift+Enter** to remove it; the panel emits
:attr:`TagPanel.tag_requested` and the window does the tagging. In the filter box the text
is then selected, so typing the next tag replaces it. Typing letters in the tree goes to the
filter box. The tag tree is loaded in a worker; call :meth:`TagPanel.reload` after tags
change.
"""

import shiboken6
from PySide6.QtCore import QEvent, QModelIndex, QObject, QPoint, Qt, QThreadPool, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.session import KeepSession
from tagalot.core.tags import PATH_SEPARATOR, TagError, TagTree, split_tag_path
from tagalot.ui.models.tag_tree import TagTreeModel
from tagalot.ui.workers import run_in_pool


class TagPanel(QWidget):
    """Filter box and tag tree. Emits :attr:`current_changed` with the highlighted tag id
    (or ``None``)."""

    current_changed = Signal(object)
    tag_requested = Signal(list, bool)
    """Tag ids, and whether to remove them (Shift+Enter) rather than apply them (Enter)."""
    create_requested = Signal(str)
    """The filter text, to create as a tag (a path like "Places > Norway" is allowed)."""
    search_requested = Signal(list, bool)
    """Tag ids to add to the current search, and whether as "but not" (from the menu)."""

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
        self.model.check_clicked.connect(
            lambda tag_id, remove: self.tag_requested.emit([tag_id], remove)
        )
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
        self.view.installEventFilter(self)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(
            lambda point: self._show_menu(
                self.view.indexAt(point), self.view.viewport().mapToGlobal(point)
            )
        )
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
        self.selection_label = QLabel()
        self.selection_label.setWordWrap(True)
        self.selection_label.hide()
        layout.addWidget(self.selection_label)
        # With nothing to show, the tree hides and these sit right under the filter box.
        layout.addWidget(self.message)
        self.create_button = QPushButton()
        self.create_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.create_button.setToolTip(
            "Create this tag (Enter). Use \u201c>\u201d to put it under another: Places > Norway"
        )
        self.create_button.clicked.connect(self.request_create)
        self.create_button.hide()
        layout.addWidget(self.create_button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.view, 1)
        layout.addStretch(0)  # takes the space while the tree is hidden
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
        self.view.setVisible(not text)
        self._show_create()

    def _show_create(self) -> None:
        """Offer "Create tag '…'" while the filter matches nothing (§12)."""
        offer = self.model.tree is not None and self.model.filtering and not self.model.matches()
        if offer:
            try:  # show it as it will be created: "Places > Norway" goes under Places
                shown = PATH_SEPARATOR.join(split_tag_path(self.filter_edit.text()))
            except TagError:
                shown = " ".join(self.filter_edit.text().split())
            selected = self.model.selected_count
            label = f"Create tag \u201c{shown}\u201d"
            if selected:
                label += f" and add it to {'1 item' if selected == 1 else f'{selected:,} items'}"
            self.create_button.setText(label)
        self.create_button.setVisible(offer)

    def request_create(self) -> None:
        """Ask to create the filter text as a tag (and apply it to the selection)."""
        text = self.filter_edit.text().strip()
        if text and self.create_button.isVisible():
            self.create_requested.emit(text)

    # --- keyboard ---

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(watched, event)
        assert isinstance(event, QKeyEvent)
        key = event.key()
        enter = key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        remove = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if watched is self.filter_edit:
            if key == Qt.Key.Key_Down:
                self._focus_tree()
                return True
            if key == Qt.Key.Key_Escape and self.filter_edit.text():
                self.filter_edit.clear()
                return True
            if enter:
                if self.create_button.isVisible() and not remove:
                    self.request_create()  # nothing matches: Enter creates the tag
                else:
                    self.request(remove=remove)
                self.filter_edit.selectAll()  # typing the next tag replaces this one
                return True
        elif watched is self.view:
            if enter:
                self.request(remove=remove)
                return True
            if key == Qt.Key.Key_Escape:
                self.focus_filter()
                return True
            text = event.text()
            typing = event.modifiers() & ~Qt.KeyboardModifier.ShiftModifier
            if text and text.isprintable() and not text.isspace() and not typing:
                self.filter_edit.setFocus(Qt.FocusReason.OtherFocusReason)
                self.filter_edit.insert(text)  # type to filter, from the tree too
                return True
        return super().eventFilter(watched, event)

    # --- the context menu ---

    def menu_for(self, index: QModelIndex) -> QMenu | None:
        """The right-click menu for the tag at ``index`` (with the other selected tags, if
        it is one of them), or ``None`` off the tags."""
        clicked = self.model.tag_id(index)
        tree = self.model.tree
        if clicked is None or tree is None:
            return None
        selected = [
            t
            for i in self.view.selectionModel().selectedIndexes()
            if (t := self.model.tag_id(i)) is not None
        ]
        tags = list(dict.fromkeys(selected)) if clicked in selected else [clicked]
        names = repr(tree.display_name(tags[0])) if len(tags) == 1 else f"{len(tags)} tags"
        menu = QMenu(self)
        menu.addAction(
            f"Show only items with {names}", lambda: self.search_requested.emit(tags, False)
        )
        menu.addAction(f"Hide items with {names}", lambda: self.search_requested.emit(tags, True))
        menu.addSeparator()
        count = self.model.selected_count
        items = "the selected item" if count == 1 else f"the {count:,} selected items"
        add = menu.addAction(
            f"Add {names} to {items}", lambda: self.tag_requested.emit(tags, False)
        )
        remove = menu.addAction(
            f"Remove {names} from {items}", lambda: self.tag_requested.emit(tags, True)
        )
        for action in (add, remove):
            action.setEnabled(bool(count))
        if not count:
            add.setText(f"Add {names} to the selected items")
            remove.setText(f"Remove {names} from the selected items")
        return menu

    def _show_menu(self, index: QModelIndex, where: QPoint) -> None:
        menu = self.menu_for(index)
        if menu is not None:
            menu.exec(where)

    def target_tags(self) -> list[int]:
        """The tags Enter would use: those selected in the tree, else the highlighted one,
        else (while filtering) the first match."""
        selected = [
            t
            for i in self.view.selectionModel().selectedIndexes()
            if (t := self.model.tag_id(i)) is not None
        ]
        if selected:
            return list(dict.fromkeys(selected))
        current = self.current_tag()
        if current is not None:
            return [current]
        first = self.model.first_match() if self.model.filtering else None
        tag_id = self.model.tag_id(first) if first is not None else None
        return [tag_id] if tag_id is not None else []

    def request(self, *, remove: bool = False) -> None:
        """Ask to apply (or remove) :meth:`target_tags` to the selected items."""
        tags = self.target_tags()
        if tags:
            self.tag_requested.emit(tags, remove)

    def set_selection(
        self,
        selected_count: int,
        tag_counts: dict[int, int],
        types: frozenset[str] = frozenset(),
    ) -> None:
        """Show which tags the selected items have (checked: all, partly: some), hiding
        tags limited to types none of them are (#135)."""
        current = self.current_tag()
        if self.model.set_selection(selected_count, tag_counts, types):
            self._after_reset()
            if current is not None:
                self.select_tag(current)
        items = "1 item" if selected_count == 1 else f"{selected_count:,} items"
        self.selection_label.setText(
            f"{items} selected. Tick a tag to put it on all of them; untick it to remove it."
        )
        self.selection_label.setVisible(bool(selected_count))
        self._show_create()

    def focus_filter(self) -> None:
        """Put the cursor in the filter box with its text selected (Ctrl+T)."""
        self.filter_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.filter_edit.selectAll()

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
