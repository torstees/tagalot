"""The tagging panel: the tag tree with a filter box (DESIGN.md §12 "Tagging panel").

Typing in the filter box narrows the tree to matching tags (names, aliases, descriptions)
and their ancestors, expanded so every match shows; clearing it restores the folds the user
had. Down moves into the tree, on the first match. Every tag starts expanded; the buttons
beside the filter box (and the right-click menu) expand or collapse them all (#490).

Keyboard tagging: **Enter** asks to apply the highlighted tag (or the tags selected in the
tree) to the selected items, **Shift+Enter** to remove it; the panel emits
:attr:`TagPanel.tag_requested` and the window does the tagging. In the filter box the text
is then selected, so typing the next tag replaces it. Typing letters in the tree goes to the
filter box. The tag tree is loaded in a worker; call :meth:`TagPanel.reload` after tags
change.

**Tag groups** (#535): **Groups ▾** beside the filter box shows every tag, or only the groups
ticked, each as a heading over its tags; while filtering, matches outside them follow under
**Other tags**. Right-click a tag → **Groups ▸** puts it in a group or takes it out, or
makes a new group with it. Which groups show is the user's (the window keeps it in
``ui_state.json``).

**Only their tags** (#541): the checked box button beside the filter box shows only the tags
the selected items have, with their parents, so how they're tagged shows at a glance; with
groups shown, their tags outside the groups follow under **Other tags**, greyed.
"""

import html
from collections.abc import Iterable

import shiboken6
from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QPointF,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import QIcon, QKeyEvent, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolButton,
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
    back_requested = Signal()
    """**Back to …'s tags**: clear the selection in the page's contents, so the checks are
    the page's own item's again (#504)."""
    groups_changed = Signal(object)
    """The user chose which tag groups show (#535): a list of group ids, or ``None`` for
    every tag."""
    group_requested = Signal(int, list, bool)
    """Put tags in a group (``True``) or take them out: group id, tag ids."""
    new_group_requested = Signal(str, list)
    """Make a group, by this name, holding these tags."""

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
        self._closed_headings: set[str] = set()  # group headings the user folded (#535)
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
        self._icon_size = self.view.iconSize()
        self.model.modelReset.connect(self._fit_icons)
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

        self.expand_button = QToolButton()
        self.expand_button.setIcon(fold_icon(expand=True))
        self.expand_button.setAutoRaise(True)
        self.expand_button.setToolTip("Expand all tags")
        self.expand_button.clicked.connect(self.expand_all)
        self.collapse_button = QToolButton()
        self.collapse_button.setIcon(fold_icon(expand=False))
        self.collapse_button.setAutoRaise(True)
        self.collapse_button.setToolTip("Collapse all tags")
        self.collapse_button.clicked.connect(self.collapse_all)

        self.applied_button = QToolButton()
        self.applied_button.setCheckable(True)
        self.applied_button.setAutoRaise(True)
        self.applied_button.setIcon(checked_icon())
        self.applied_button.setToolTip(
            "Show only the tags the selected items have (click again for every tag)"
        )
        self.applied_button.toggled.connect(self.set_applied_only)

        self.groups_button = QToolButton()
        self.groups_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.groups_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.groups_menu = QMenu(self.groups_button)
        self.groups_menu.aboutToShow.connect(self._fill_groups_menu)
        self.groups_button.setMenu(self.groups_menu)
        self._show_groups_button()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        top.setSpacing(2)
        top.addWidget(self.filter_edit, 1)
        top.addWidget(self.applied_button)
        top.addWidget(self.groups_button)
        top.addWidget(self.expand_button)
        top.addWidget(self.collapse_button)
        layout.addLayout(top)
        self.selection_label = QLabel()
        self.selection_label.setWordWrap(True)
        self.selection_label.setTextFormat(Qt.TextFormat.RichText)
        self.selection_label.linkActivated.connect(lambda _: self.back_requested.emit())
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
        scrolled = self.view.verticalScrollBar().value()
        self._expanded &= set(_all_ids(tree))  # forget deleted tags
        if first_load:
            self._expanded = set(_all_ids(tree))  # start with every tag open (#490)
        self.model.set_tree(tree)
        self._after_reset()
        self._restore_place(current, scrolled)
        self._show_groups_button()  # a group renamed or deleted

    # --- state ---

    def _fit_icons(self) -> None:
        """Room for a swatch and an icon picture side by side, while some tag's icon is a
        picture (#507)."""
        size = self.model.decoration_size()
        self.view.setIconSize(size if size is not None else self._icon_size)

    def current_tag(self) -> int | None:
        return self.model.tag_id(self.view.currentIndex())

    def select_tag(self, tag_id: int, *, scroll: bool = True) -> bool:
        """Highlight a shown tag, scrolling to it unless ``scroll`` is false; returns
        whether it is shown."""
        index = self.model.index_of(tag_id)
        if not index.isValid():
            return False
        self.view.setCurrentIndex(index)
        if scroll:
            self.view.scrollTo(index)
        return True

    def _restore_place(self, current: int | None, scrolled: int) -> None:
        """After the tree was made again (new counts after a tick, a reload): the same tag
        highlighted, and the tree where it was, not scrolled to the highlight, which can be
        far from the box just ticked."""
        if current is not None:
            self.select_tag(current, scroll=False)
        self.view.verticalScrollBar().setValue(scrolled)

    def expanded_tags(self) -> set[int]:
        return set(self._expanded)

    def expand_all(self) -> None:
        """Open every tag (#490). While filtering, the tree shown is already open: this
        becomes the folds the user gets back."""
        tree = self.model.tree
        if tree is None:
            return
        self._expanded = set(_all_ids(tree))
        if not self.model.narrowing:
            self._restoring = True
            try:
                self.view.expandAll()
            finally:
                self._restoring = False

    def collapse_all(self) -> None:
        """Close every tag, leaving the top level (#490): with groups showing, the
        headings stay open, so their tags show."""
        self._expanded = set()
        self._closed_headings = set()
        if not self.model.narrowing:
            self._restoring = True
            try:
                self.view.collapseAll()
                for heading in self.model.headings():
                    self.view.expand(heading)
            finally:
                self._restoring = False

    # --- only the selected items' tags (#541) ---

    def set_applied_only(self, on: bool) -> None:
        """Show only the tags the selected items have, or every tag."""
        if self.applied_button.isChecked() != on:
            self.applied_button.setChecked(on)  # comes back here through toggled
            return
        current = self.current_tag()
        self.model.set_applied_only(on)
        self._after_reset()
        if current is not None:
            self.select_tag(current)

    # --- tag groups (#535) ---

    def set_groups(self, groups: Iterable[int] | None, *, by_user: bool = False) -> None:
        """Show only these tag groups, as headings over their tags, or every tag
        (``None``). ``by_user``: the user chose, so :attr:`groups_changed` says so."""
        chosen = None if groups is None else list(dict.fromkeys(groups)) or None
        current = self.current_tag()
        self.model.set_groups(chosen)
        self._after_reset()
        if current is not None:
            self.select_tag(current)
        self._show_groups_button()
        if by_user:
            self.groups_changed.emit(chosen)

    def _show_groups_button(self) -> None:
        """ "All tags", the group shown, or how many, as the button's text."""
        shown = self.model.shown_groups()
        tree = self.model.tree
        if shown is None or tree is None:
            text = "All tags"
        elif len(shown) == 1:
            text = tree.group(shown[0]).name
            if len(text) > GROUP_NAME_CHARS:
                text = text[: GROUP_NAME_CHARS - 1] + "…"
        else:
            text = f"{len(shown)} groups"
        self.groups_button.setText(text)
        self.groups_button.setToolTip(
            "Which tags show: every tag, or only tag groups you choose. Right-click a tag "
            "→ Groups to put it in a group."
        )

    def _fill_groups_menu(self) -> None:
        """**All tags**, then each group, ticked when shown."""
        menu = self.groups_menu
        menu.clear()
        shown = self.model.shown_groups()
        every = menu.addAction("All tags", lambda: self.set_groups(None, by_user=True))
        every.setCheckable(True)
        every.setChecked(shown is None)
        menu.addSeparator()
        tree = self.model.tree
        groups = tree.groups() if tree is not None else ()
        for group in groups:
            action = menu.addAction(group.name)
            action.setCheckable(True)
            action.setChecked(shown is not None and group.id in shown)
            action.toggled.connect(lambda on, g=group.id: self._toggle_group(g, on))
        if not groups:
            none = menu.addAction("No groups yet: right-click a tag → Groups → New group…")
            none.setEnabled(False)

    def _toggle_group(self, group_id: int, on: bool) -> None:
        shown = list(self.model.shown_groups() or ())
        if on and group_id not in shown:
            shown.append(group_id)
        elif not on and group_id in shown:
            shown.remove(group_id)
        self.set_groups(shown or None, by_user=True)

    def groups_menu_for(self, tags: list[int], names: str) -> QMenu:
        """**Groups ▸** for these tags: each group, ticked when they are all in it, then
        **New group…**."""
        tree = self.model.tree
        menu = QMenu("Groups", self)
        for group in tree.groups() if tree is not None else ():
            action = menu.addAction(group.name)
            action.setCheckable(True)
            inside = all(t in group.tags for t in tags)
            action.setChecked(inside)
            action.setToolTip(
                f"Take {names} out of this group" if inside else f"Put {names} in this group"
            )
            action.triggered.connect(lambda on, g=group.id: self.group_requested.emit(g, tags, on))
        if not menu.isEmpty():
            menu.addSeparator()
        menu.addAction("New group…", lambda: self.ask_new_group(tags))
        menu.setToolTipsVisible(True)
        return menu

    def ask_new_group(self, tags: list[int]) -> None:
        """Ask for a name, then make a group holding ``tags``."""
        name, ok = QInputDialog.getText(self, "New group", "Name of the new tag group:")
        if ok and name.strip():
            self.new_group_requested.emit(name, tags)

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
            if self.model.narrowing:
                self.view.expandAll()
            else:
                for tag_id in self._expanded:
                    for index in self.model.indexes_of(tag_id):
                        self.view.expand(index)
                for heading in self.model.headings():
                    if self.model.heading_key(heading) not in self._closed_headings:
                        self.view.expand(heading)
        finally:
            self._restoring = False
        self._show_message()

    def _folded(self, index: object, expanded: bool) -> None:
        if self._restoring or self.model.narrowing:
            return  # folds made while filtering don't replace the user's own
        tag_id = self.model.tag_id(index)  # type: ignore[arg-type]
        if tag_id is None:
            key = self.model.heading_key(index)  # type: ignore[arg-type]
            if key is not None and expanded:
                self._closed_headings.discard(key)
            elif key is not None:
                self._closed_headings.add(key)
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
        elif self.model.applied_only and not self.model.selected_count:
            text = "Select items to see the tags they have."
        elif self.model.applied_only and not self.model.applied():
            text = "The selected items have no tags yet."
        elif self.model.applied_only and not self.model.rowCount():
            text = "The selected items have no tags that match."
        elif not self.model.rowCount():
            text = "No tags in the groups shown. Right-click a tag → Groups to add some."
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
        if tree is not None and clicked is None and index.isValid():
            return self.heading_menu(index)
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
        menu.addSeparator()
        menu.addMenu(self.groups_menu_for(tags, names))
        self.add_fold_actions(menu)
        return menu

    def heading_menu(self, index: QModelIndex) -> QMenu:
        """A group heading's menu: stop showing that group, or show every tag (#535)."""
        menu = QMenu(self)
        key = self.model.heading_key(index) or ""
        if key.startswith("group:"):
            group_id = int(key.removeprefix("group:"))
            name = index.data()
            rest = [g for g in self.model.shown_groups() or () if g != group_id]
            menu.addAction(f"Stop showing “{name}”", lambda: self.set_groups(rest, by_user=True))
        menu.addAction("Show all tags", lambda: self.set_groups(None, by_user=True))
        self.add_fold_actions(menu)
        return menu

    def fold_menu(self) -> QMenu:
        """The right-click menu off the tags: **Expand all** and **Collapse all**."""
        menu = QMenu(self)
        menu.addAction(fold_icon(expand=True), "Expand all", self.expand_all)
        menu.addAction(fold_icon(expand=False), "Collapse all", self.collapse_all)
        return menu

    def _show_menu(self, index: QModelIndex, where: QPoint) -> None:
        menu = self.menu_for(index)
        (menu if menu is not None else self.fold_menu()).exec(where)

    def add_fold_actions(self, menu: QMenu) -> None:
        """**Expand all** and **Collapse all**, at the end of a menu (#490)."""
        menu.addSeparator()
        menu.addAction(fold_icon(expand=True), "Expand all", self.expand_all)
        menu.addAction(fold_icon(expand=False), "Collapse all", self.collapse_all)

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
        file_counts: dict[int, int] | None = None,
        page_item: str | None = None,
        page_item_selected: bool = False,
    ) -> None:
        """Show which tags the selected items have (checked: all, partly: some), hiding
        tags limited to types none of them are (#135). On an item's page (``page_item``, its
        title), say whether the checks are that item's own (``page_item_selected``), or the
        selection's in its contents, with a way back (#504)."""
        current = self.current_tag()
        scrolled = self.view.verticalScrollBar().value()
        if self.model.set_selection(selected_count, tag_counts, types, file_counts):
            self._after_reset()
            self._restore_place(current, scrolled)
        items = "1 item" if selected_count == 1 else f"{selected_count:,} items"
        if page_item is not None and page_item_selected:
            name = html.escape(page_item)
            text = f"<b>{name}</b>'s tags. Tick a tag to put it on; untick it to remove it."
        else:
            text = f"{items} selected. Tick a tag to put it on all of them; untick it to remove it."
            if page_item is not None:
                text += f' <a href="back">Back to {html.escape(page_item)}\'s tags</a>'
        self.selection_label.setText(text)
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


def checked_icon(size: int = 16) -> QIcon:
    """A small drawn box with a tick in it, in the palette's text color: **Only their
    tags** (#541), like :func:`fold_icon`."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QApplication.palette().color(QPalette.ColorRole.WindowText)
    painter.setPen(color)
    painter.drawRect(2, 2, 11, 11)
    pen = painter.pen()
    pen.setWidthF(1.8)
    painter.setPen(pen)
    painter.drawPolyline([QPointF(4.5, 7.5), QPointF(7, 10.5), QPointF(11.5, 4.5)])
    painter.end()
    return QIcon(pixmap)


GROUP_NAME_CHARS = 16
"""The most of a group's name **Groups ▾** shows."""


def fold_icon(*, expand: bool, size: int = 16) -> QIcon:
    """A small drawn box with a plus (expand all) or a minus (collapse all), in the
    palette's text color, like the results' layout icons."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    color = QApplication.palette().color(QPalette.ColorRole.WindowText)
    painter.setPen(color)
    painter.drawRect(2, 2, 11, 11)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawRect(5, 7, 6, 2)  # the minus
    if expand:
        painter.drawRect(7, 5, 2, 6)  # and the plus's upright
    painter.end()
    return QIcon(pixmap)


def _all_ids(tree: TagTree) -> list[int]:
    ids: list[int] = []
    stack = list(tree.children(None))
    while stack:
        tag_id = stack.pop()
        ids.append(tag_id)
        stack.extend(tree.children(tag_id))
    return ids
