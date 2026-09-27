"""Search view: a heading, the result count, and the results (DESIGN.md §12).

Results use the list layout (columns) for now; the grid and tree layouts, and the filter bar
(#56), come later. A search page is created once per navigation target and re-runs its
search after a scan.
"""

from collections.abc import Sequence

from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.search_fields import scope_fields, scoped_tables, type_labels
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.theme_schema import ThemeSchema
from tagalot.ui.models.results import ResultColumn, ResultsModel

TITLE_WIDTH = 360
COLUMN_WIDTH = 130


def list_columns(schema: ThemeSchema, types: Sequence[str]) -> list[ResultColumn]:
    """The list layout's columns for a scope: the title (named as the types call it), the
    type when several types are in scope, then the card fields every scoped type has."""
    tables = scoped_tables(schema, types)
    labels = {t.entity.title_label for t in tables}
    columns = [ResultColumn("title", labels.pop() if len(labels) == 1 else "Title")]
    if len(tables) != 1:
        columns.append(ResultColumn("type", "Type", sortable=False))
    columns.extend(
        ResultColumn(f.name, f.spec.label, numeric=f.type in (int, float))
        for f in scope_fields(schema, types)
        if f.spec.card
    )
    return columns


def count_text(total: int) -> str:
    return f"{total:,} item" if total == 1 else f"{total:,} items"


class SearchPage(QWidget):
    """One search: ``title`` above a list of the entities ``spec`` finds."""

    def __init__(
        self,
        session: KeepSession,
        title: str,
        spec: SearchSpec,
        *,
        pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self._showing_sort = False  # true while the page itself moves the sort indicator
        self.model = ResultsModel(session, type_labels=type_labels(session.schema), pool=pool)
        self.model.setParent(self)

        heading = QLabel(title)
        font = QFont(heading.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 2)
        heading.setFont(font)
        self.status = QLabel("Searching…")
        header_row = QHBoxLayout()
        header_row.addWidget(heading)
        header_row.addStretch(1)
        header_row.addWidget(self.status)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        # Fixed row heights and column widths: sizing to contents would read every row.
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 8)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        header.sortIndicatorChanged.connect(self._sort_clicked)

        layout = QVBoxLayout(self)
        layout.addLayout(header_row)
        layout.addWidget(self.table, 1)

        self.model.counted.connect(self._counted)
        self.model.failed.connect(self._failed)
        self.model.modelReset.connect(self._show_sort)
        self.model.set_search(spec, list_columns(session.schema, spec.types))
        for i, column in enumerate(self.model.columns):
            self.table.setColumnWidth(i, TITLE_WIDTH if column.key == "title" else COLUMN_WIDTH)

    def refresh(self) -> None:
        """Run the search again (after a scan), keeping the list until new rows arrive."""
        self.model.refresh()

    def _counted(self, total: int) -> None:
        self.status.setText(count_text(total) if total else "Nothing found")

    def _failed(self, message: str) -> None:
        self.status.setText(message)

    def _sort_clicked(self, column: int, _order: object) -> None:
        if self._showing_sort:
            return
        self.model.sort(column, self.table.horizontalHeader().sortIndicatorOrder())
        self._show_sort()  # a column that can't sort puts the indicator back

    def _show_sort(self) -> None:
        current = self.model.sort_column()
        header = self.table.horizontalHeader()
        self._showing_sort = True
        try:
            if current is None:
                header.setSortIndicator(-1, header.sortIndicatorOrder())
            else:
                header.setSortIndicator(*current)
        finally:
            self._showing_sort = False
        if self.model.searching:
            self.status.setText("Searching…")
