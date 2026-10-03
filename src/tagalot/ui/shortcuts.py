"""Help → Keyboard shortcuts (#128): every key the window answers to, in one place.

The menu commands' keys are read from the window's menus as they are (so the list can't
drift from them); keys that work inside one kind of widget (the results, the tag box, the
Tags panel, a field being edited, the navigation) are listed in :data:`IN_PLACE`. Keys are
shown the platform's way (⌘ rather than Ctrl on a Mac).
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLineEdit,
    QMainWindow,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

IN_PLACE: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Results: lists, grids, and trees",
        [
            ("Enter", "Open the item's page (or its file, for items that open as files)"),
            ("Ctrl+Enter", "The other one: the file, or the page"),
            ("Double-click", "Same as Enter"),
            ("Ctrl+F", "Find: the cursor to the search box"),
            ("Ctrl+wheel", "In a grid: bigger or smaller thumbnails"),
        ],
    ),
    (
        "The search box's tags",
        [
            ("Up, Down", "Move through the suggested tags"),
            ("Enter", "Add the highlighted tag as a chip"),
            ("Shift+Enter", "Add it as a “but not” chip"),
            ("Esc", "Close the suggestions"),
        ],
    ),
    (
        "Tags panel",
        [
            ("Type", "Filter the tags (from the tree too)"),
            ("Down", "From the filter into the tree"),
            ("Enter", "Apply the highlighted (or selected) tags to the selection"),
            ("Shift+Enter", "Remove them from the selection"),
            ("Enter, with no match", "Create the tag typed"),
            ("Esc", "Clear the filter, or go back to it from the tree"),
        ],
    ),
    (
        "Editing a field on an item's page",
        [
            ("Enter", "Save the value"),
            ("Esc", "Leave it as it was"),
        ],
    ),
    (
        "Navigation pane",
        [
            ("Up, Down", "Move between pages"),
            ("Left, Right", "Fold or unfold a heading"),
        ],
    ),
]
"""Keys that work inside one kind of widget, by where, as (keys, what they do)."""


def keys_text(keys: str) -> str:
    """Keys written the platform's way: ``Ctrl+Enter`` stays on Windows and Linux and is
    ``⌘Enter`` on a Mac. Text that isn't a key sequence (``Double-click``) is kept."""
    parts = [p.strip() for p in keys.split(",")]
    shown = []
    for part in parts:
        sequence = QKeySequence(part)
        native = sequence.toString(QKeySequence.SequenceFormat.NativeText)
        valid = not sequence.isEmpty() and "+" in part and "click" not in part.lower()
        shown.append(native if valid and native else part)
    return ", ".join(shown)


def menu_shortcuts(window: QMainWindow) -> list[tuple[str, list[tuple[str, str]]]]:
    """Each menu's commands that have keys: (menu title, [(keys, command)])."""
    found = []
    for top in window.menuBar().actions():
        menu = top.menu()
        if not isinstance(menu, QMenu):
            continue
        rows = list(_actions_with_keys(menu))
        if rows:
            found.append((_plain(top.text()), rows))
    return found


def _actions_with_keys(menu: QMenu) -> list[tuple[str, str]]:
    rows = []
    for action in menu.actions():
        sub = action.menu()
        if isinstance(sub, QMenu):
            rows += _actions_with_keys(sub)
            continue
        keys = [
            k.toString(QKeySequence.SequenceFormat.NativeText)
            for k in action.shortcuts()
            if not k.isEmpty()
        ]
        if keys and not action.isSeparator():
            rows.append((", ".join(dict.fromkeys(keys)), _plain(action.text())))
    return rows


def _plain(text: str) -> str:
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&").rstrip("…").strip()


class ShortcutsDialog(QDialog):
    """See the module docstring. Typing in the box narrows the list."""

    def __init__(self, window: QMainWindow, parent: QWidget | None = None) -> None:
        super().__init__(parent or window)
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(680, 640)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Find a key or a command")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._narrow)
        self.tree = QTreeWidget()
        self.tree.setObjectName("shortcuts")
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Keys", "What they do"])
        self.tree.setRootIsDecorated(False)
        self.tree.setWordWrap(True)  # long descriptions wrap rather than being cut off
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._groups: list[tuple[QTreeWidgetItem, list[QTreeWidgetItem]]] = []
        groups = [(f"Menu: {title}", rows) for title, rows in menu_shortcuts(window)]
        groups += [(title, [(keys_text(k), what) for k, what in rows]) for title, rows in IN_PLACE]
        for title, rows in groups:
            heading = QTreeWidgetItem([title])
            heading.setFirstColumnSpanned(True)
            font = heading.font(0)
            font.setBold(True)
            heading.setFont(0, font)
            heading.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(heading)
            heading.setFirstColumnSpanned(True)
            children = [QTreeWidgetItem([keys, what]) for keys, what in rows]
            heading.addChildren(children)
            self._groups.append((heading, children))
        self.tree.expandAll()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.search_box)
        layout.addWidget(self.tree, 1)
        layout.addWidget(buttons)

    def rows(self) -> list[tuple[str, str, str]]:
        """(group, keys, what) of every row shown (for tests)."""
        return [
            (heading.text(0), child.text(0), child.text(1))
            for heading, children in self._groups
            if not heading.isHidden()
            for child in children
            if not child.isHidden()
        ]

    def _narrow(self, text: str) -> None:
        needle = text.strip().casefold()
        for heading, children in self._groups:
            group = needle in heading.text(0).casefold()
            any_shown = False
            for child in children:
                hit = (
                    not needle
                    or group
                    or needle in child.text(0).casefold()
                    or needle in child.text(1).casefold()
                )
                child.setHidden(not hit)
                any_shown |= hit
            heading.setHidden(not any_shown)


def help_action(window: QMainWindow) -> QAction:
    """Help → Keyboard shortcuts (F1, Ctrl+/)."""
    action = QAction("Keyboard shortcuts…", window)
    action.setShortcuts([QKeySequence(Qt.Key.Key_F1), QKeySequence("Ctrl+/")])
    action.setToolTip("Every key Tagalot answers to")
    return action
