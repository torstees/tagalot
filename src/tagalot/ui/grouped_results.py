"""The global search's results, grouped by type (DESIGN.md §8 "Global vs. scoped", §12).

```
▾ Album (40)
   Title                  Year
   Love Deluxe            1992
   …
   Show all 40 →
▸ Song (812)
```

Each type with matches gets a collapsible section: its count, its first few matches with
that type's own columns, and "Show all N →" when there are more. :func:`load_groups` runs in
a worker; :class:`GroupedResults` shows its result.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QPoint, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.search import count_by_type, run_search
from tagalot.core.search_fields import scoped_tables, search_fields, type_plurals
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.ui.file_actions import add_open_keys
from tagalot.ui.models.results import PreviewModel, ResultColumn, Row, row_values
from tagalot.ui.result_table import list_columns, make_result_table, set_column_widths

PREVIEW_ROWS = 8
"""How many matches each section shows before "Show all"."""


@dataclass(frozen=True)
class TypeGroup:
    """One section: a type, how many entities of it match, and the first few."""

    type_id: str
    label: str
    """The plural display name, used as the section heading ("Albums")."""
    count: int
    columns: tuple[ResultColumn, ...]
    rows: tuple[Row, ...]


def load_groups(
    session: KeepSession, spec: SearchSpec, limit: int = PREVIEW_ROWS
) -> list[TypeGroup]:
    """Count ``spec``'s matches per type and load each type's first ``limit`` matches, in the
    theme's type order. Types without matches are left out. Runs in a worker."""
    schema = session.schema
    labels = type_plurals(schema)
    tree = session.tag_cache.get()
    groups = []
    with session.reader.connect() as conn:  # one read transaction: counts and rows agree
        counts = count_by_type(conn, spec, tree, search_fields(schema, spec.types))
        for table in scoped_tables(schema, spec.types):
            count = counts.get(table.type_id, 0)
            if not count:
                continue
            types = (table.type_id,)
            columns = list_columns(schema, types)
            hits = run_search(
                conn,
                replace(spec, types=types),
                tree,
                limit=limit,
                fields=search_fields(schema, types),
            )
            values = row_values(conn, schema, tree, hits, columns)
            rows = tuple((h, values.get(h.id, {})) for h in hits)
            groups.append(
                TypeGroup(table.type_id, labels[table.type_id], count, tuple(columns), rows)
            )
    return groups


class TypeSection(QWidget):
    """A collapsible section for one type. Emits :attr:`show_all` with the type id, and
    :attr:`tags_dropped` with entity ids and tag ids when tags are dropped on its rows."""

    show_all = Signal(str)
    toggled = Signal(str, bool)
    tags_dropped = Signal(list, list)
    column_toggled = Signal(str, bool)
    hit_activated = Signal(object, bool)
    """Double-click or Enter on a row (``False``), or Ctrl+Enter (``True``)."""
    """A row was double-clicked (or Enter pressed): the entity id."""
    item_menu_requested = Signal(object, QPoint)
    """A row was right-clicked: its :class:`SearchHit` and the global position."""

    def __init__(
        self,
        group: TypeGroup,
        expanded: bool = True,
        hidden_columns: Iterable[str] = (),
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.group = group
        self.header = QToolButton()
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setAutoRaise(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        font = QFont(self.header.font())
        font.setBold(True)
        self.header.setFont(font)
        # A heading, not a pressed button: no checked-state highlight.
        self.header.setStyleSheet(
            "QToolButton { border: none; background: transparent; color: palette(window-text); }"
        )
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setToolTip("Fold or unfold this section")
        self.header.toggled.connect(self._toggled)

        self.model = PreviewModel(group.columns, group.rows, parent=self)
        self.table = make_result_table()
        self.table.setModel(self.model)
        self.table.tags_dropped.connect(self._tags_dropped)
        set_column_widths(self.table, group.columns)
        self.table.set_columns(group.columns, hidden_columns)
        self.table.column_toggled.connect(self.column_toggled)
        self.table.activated.connect(self._activated)
        add_open_keys(self.table, self._activated)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rows_height = sum(self.table.rowHeight(r) for r in range(self.model.rowCount()))
        frame = 2 * self.table.frameWidth()
        self.table.setFixedHeight(
            self.table.horizontalHeader().sizeHint().height() + rows_height + frame
        )

        self.show_all_button = QPushButton(f"Show all {group.count:,} →")
        self.show_all_button.setFlat(True)
        self.show_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_all_button.setToolTip(f"List all {group.count:,} {group.label}, with sorting")
        self.show_all_button.clicked.connect(lambda: self.show_all.emit(group.type_id))
        self.show_all_button.setVisible(group.count > len(group.rows))
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addWidget(self.show_all_button)
        footer.addStretch(1)
        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(18, 0, 0, 0)
        body_layout.setSpacing(2)
        body_layout.addWidget(self.table)
        body_layout.addLayout(footer)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.header, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.body)
        self._toggled(expanded, notify=False)

    def _menu(self, point: QPoint) -> None:
        hit = self.model.hit(self.table.indexAt(point).row())
        if hit is not None:
            self.item_menu_requested.emit(hit, self.table.viewport().mapToGlobal(point))

    def _activated(self, index: QModelIndex, alternate: bool = False) -> None:
        hit = self.model.hit(index.row())
        if hit is not None:
            self.hit_activated.emit(hit, alternate)

    def _tags_dropped(self, rows: list[int], tag_ids: list[int]) -> None:
        ids = [hit.id for r in rows if (hit := self.model.hit(r)) is not None]
        if ids:
            self.tags_dropped.emit(ids, tag_ids)

    @property
    def expanded(self) -> bool:
        return self.header.isChecked()

    def _toggled(self, expanded: bool, notify: bool = True) -> None:
        arrow = "▾" if expanded else "▸"
        self.header.setText(f"{arrow} {self.group.label} ({self.group.count:,})")
        self.body.setVisible(expanded)
        if notify:
            self.toggled.emit(self.group.type_id, expanded)


class GroupedResults(QScrollArea):
    """The sections, one per type, in a scroll area. Emits :attr:`show_all` with a type id.

    Sections the user folds stay folded when the results are replaced (new filters, a scan).
    """

    show_all = Signal(str)
    tags_dropped = Signal(list, list)
    column_toggled = Signal(str, bool)
    hit_activated = Signal(object, bool)
    """Double-click or Enter on a row (``False``), or Ctrl+Enter (``True``)."""
    item_menu_requested = Signal(object, QPoint)
    selection_changed = Signal()
    """Any section's selection changed (or the sections were replaced)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._folded: set[str] = set()
        self.hidden_columns: set[str] = set()
        """Column keys hidden in every section (the page's choice)."""
        self.sections: list[TypeSection] = []
        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._layout.addStretch(1)
        self.setWidget(self._content)

    def set_groups(self, groups: Sequence[TypeGroup]) -> None:
        """Replace the sections, keeping selected the items that are still shown."""
        kept = {
            hit.id
            for section in self.sections
            for index in section.table.selectionModel().selectedRows()
            if (hit := section.model.hit(index.row())) is not None
        }
        for section in self.sections:
            self._layout.removeWidget(section)
            section.deleteLater()
        self.sections = []
        for group in groups:
            expanded = group.type_id not in self._folded
            section = TypeSection(group, expanded, self.hidden_columns)
            section.show_all.connect(self.show_all)
            section.tags_dropped.connect(self.tags_dropped)
            section.column_toggled.connect(self.column_toggled)
            section.hit_activated.connect(self.hit_activated)
            section.item_menu_requested.connect(self.item_menu_requested)
            section.table.selectionModel().selectionChanged.connect(self.selection_changed)
            section.toggled.connect(self._remember_fold)
            self._layout.insertWidget(self._layout.count() - 1, section)
            self.sections.append(section)
            for row, (hit, _) in enumerate(group.rows):
                if hit.id in kept:
                    section.table.selectionModel().select(
                        section.model.index(row, 0),
                        QItemSelectionModel.SelectionFlag.Select
                        | QItemSelectionModel.SelectionFlag.Rows,
                    )
        self.selection_changed.emit()

    def set_hidden_columns(self, hidden: Iterable[str]) -> None:
        """Hide ``hidden`` in every section, now and in later results."""
        self.hidden_columns = set(hidden)
        for section in self.sections:
            section.table.set_columns(section.group.columns, self.hidden_columns)

    def _remember_fold(self, type_id: str, expanded: bool) -> None:
        if expanded:
            self._folded.discard(type_id)
        else:
            self._folded.add(type_id)
