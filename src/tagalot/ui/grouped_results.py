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

from collections.abc import Sequence
from dataclasses import dataclass, replace

from PySide6.QtCore import Qt, Signal
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
from tagalot.core.search_fields import field_values, scoped_tables, search_fields, type_labels
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.ui.models.results import PreviewModel, ResultColumn, Row
from tagalot.ui.result_table import list_columns, make_result_table, set_column_widths

PREVIEW_ROWS = 8
"""How many matches each section shows before "Show all"."""


@dataclass(frozen=True)
class TypeGroup:
    """One section: a type, how many entities of it match, and the first few."""

    type_id: str
    label: str
    count: int
    columns: tuple[ResultColumn, ...]
    rows: tuple[Row, ...]


def load_groups(
    session: KeepSession, spec: SearchSpec, limit: int = PREVIEW_ROWS
) -> list[TypeGroup]:
    """Count ``spec``'s matches per type and load each type's first ``limit`` matches, in the
    theme's type order. Types without matches are left out. Runs in a worker."""
    schema = session.schema
    labels = type_labels(schema)
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
            names = [c.key for c in columns if c.key not in ("title", "type")]
            values = field_values(conn, schema, hits, names) if names else {}
            rows = tuple((h, values.get(h.id, {})) for h in hits)
            groups.append(
                TypeGroup(table.type_id, labels[table.type_id], count, tuple(columns), rows)
            )
    return groups


class TypeSection(QWidget):
    """A collapsible section for one type. Emits :attr:`show_all` with the type id."""

    show_all = Signal(str)
    toggled = Signal(str, bool)

    def __init__(self, group: TypeGroup, expanded: bool = True, parent: QWidget | None = None):
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
        set_column_widths(self.table, group.columns)
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
        self.show_all_button.setToolTip(f"List every matching {group.label}, with sorting")
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._folded: set[str] = set()
        self.sections: list[TypeSection] = []
        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._layout.addStretch(1)
        self.setWidget(self._content)

    def set_groups(self, groups: Sequence[TypeGroup]) -> None:
        for section in self.sections:
            self._layout.removeWidget(section)
            section.deleteLater()
        self.sections = []
        for group in groups:
            section = TypeSection(group, expanded=group.type_id not in self._folded)
            section.show_all.connect(self.show_all)
            section.toggled.connect(self._remember_fold)
            self._layout.insertWidget(self._layout.count() - 1, section)
            self.sections.append(section)

    def _remember_fold(self, type_id: str, expanded: bool) -> None:
        if expanded:
            self._folded.discard(type_id)
        else:
            self._folded.add(type_id)
