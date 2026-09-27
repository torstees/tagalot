"""The left navigation pane: a grouped list with collapsible headings (DESIGN.md §12).

Sections: LIBRARY (Dashboard, Search all), SEARCHES (the theme's search views), SAVED (saved
searches), TOOLS (Triage, Dedupe, Tag manager). Clicking a heading folds it; a folded heading
shows how many items it hides. Few items, so a standard item model is appropriate here.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QAbstractItemView, QStyle, QTreeView, QWidget

TargetKind = Literal["dashboard", "search", "view", "saved", "triage", "dedupe", "tags"]
SECTIONS = ("library", "searches", "saved", "tools")
_TITLES = {"library": "LIBRARY", "searches": "SEARCHES", "saved": "SAVED", "tools": "TOOLS"}
_SECTION = Qt.ItemDataRole.UserRole + 1
_TARGET = Qt.ItemDataRole.UserRole + 2


@dataclass(frozen=True)
class NavTarget:
    """Something the center of the window can show."""

    kind: TargetKind
    key: str = ""
    """The view name, or the saved search id."""
    label: str = ""


class NavigationPane(QTreeView):
    """Emits :attr:`navigate` with a :class:`NavTarget`; :attr:`folded_changed` with the
    folded section keys whenever a heading folds or unfolds."""

    navigate = Signal(object)
    folded_changed = Signal(list)

    def __init__(self, views: Sequence[str] = (), parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)  # the headings carry their own ▾/▸ marker
        self.setIndentation(14)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._headings: dict[str, QStandardItem] = {}
        style = self.style()
        icons = {
            "dashboard": QStyle.StandardPixmap.SP_ComputerIcon,
            "search": QStyle.StandardPixmap.SP_FileDialogContentsView,
            "view": QStyle.StandardPixmap.SP_FileDialogListView,
            "saved": QStyle.StandardPixmap.SP_DialogSaveButton,
            "triage": QStyle.StandardPixmap.SP_MessageBoxWarning,
            "dedupe": QStyle.StandardPixmap.SP_FileDialogDetailedView,
            "tags": QStyle.StandardPixmap.SP_FileDialogInfoView,
        }
        self._icons = {kind: style.standardIcon(pixmap) for kind, pixmap in icons.items()}

        for key in SECTIONS:
            heading = QStandardItem()
            heading.setData(key, _SECTION)
            heading.setFlags(Qt.ItemFlag.ItemIsEnabled)  # not selectable
            font = QFont(heading.font())
            font.setBold(True)
            font.setPointSizeF(max(font.pointSizeF() - 1, 7))
            heading.setFont(font)
            self._model.appendRow(heading)
            self._headings[key] = heading
        self._fill(
            "library",
            [NavTarget("dashboard", label="Dashboard"), NavTarget("search", label="Search all")],
        )
        self._fill("searches", [NavTarget("view", key=v, label=v) for v in views])
        self._fill(
            "tools",
            [
                NavTarget("triage", label="Triage"),
                NavTarget("dedupe", label="Dedupe"),
                NavTarget("tags", label="Tag manager"),
            ],
        )
        self.expandAll()
        self._refresh_headings()

        self.clicked.connect(self._clicked)
        self.expanded.connect(self._fold_state_changed)
        self.collapsed.connect(self._fold_state_changed)
        self.selectionModel().currentChanged.connect(self._current_changed)

    # --- content ---

    def set_saved(self, saved: Iterable[tuple[int, str]]) -> None:
        """Replace the SAVED section with ``(id, name)`` pairs."""
        folded = self.folded()
        self._fill("saved", [NavTarget("saved", key=str(i), label=name) for i, name in saved])
        if "saved" not in folded:
            self.setExpanded(self._headings["saved"].index(), True)
        self._refresh_headings()

    def targets(self, section: str) -> list[NavTarget]:
        heading = self._headings[section]
        return [heading.child(row).data(_TARGET) for row in range(heading.rowCount())]

    def select(self, target: NavTarget) -> None:
        """Select an item programmatically (emits :attr:`navigate`)."""
        for heading in self._headings.values():
            for row in range(heading.rowCount()):
                child = heading.child(row)
                if child.data(_TARGET) == target:
                    self.setCurrentIndex(child.index())
                    return

    # --- folding ---

    def folded(self) -> set[str]:
        return {k for k, h in self._headings.items() if not self.isExpanded(h.index())}

    def set_folded(self, keys: Iterable[str]) -> None:
        wanted = set(keys)
        for key, heading in self._headings.items():
            self.setExpanded(heading.index(), key not in wanted)
        self._refresh_headings()

    # --- internals ---

    def _fill(self, section: str, targets: Sequence[NavTarget]) -> None:
        heading = self._headings[section]
        heading.removeRows(0, heading.rowCount())
        for target in targets:
            item = QStandardItem(self._icons[target.kind], target.label)
            item.setData(target, _TARGET)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            heading.appendRow(item)

    def _refresh_headings(self) -> None:
        for key, heading in self._headings.items():
            folded = not self.isExpanded(heading.index())
            count = f" ({heading.rowCount()})" if folded else ""
            heading.setText(f"{'▸' if folded else '▾'} {_TITLES[key]}{count}")

    def _clicked(self, index: QModelIndex) -> None:
        if index.data(_SECTION) is not None:
            self.setExpanded(index, not self.isExpanded(index))

    def _fold_state_changed(self, index: QModelIndex) -> None:
        self._refresh_headings()
        self.folded_changed.emit(sorted(self.folded()))

    def _current_changed(
        self, current: QModelIndex | QPersistentModelIndex, previous: object
    ) -> None:
        target = current.data(_TARGET)
        if isinstance(target, NavTarget):
            self.navigate.emit(target)
