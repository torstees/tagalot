"""The list layout's columns and table setup, shared by search pages and grouped sections."""

from collections.abc import Sequence

from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView, QWidget

from tagalot.core.search_fields import scope_fields, scoped_tables
from tagalot.core.theme_schema import ThemeSchema
from tagalot.ui.models.results import ResultColumn

TITLE_MIN_WIDTH = 200
COLUMN_WIDTH = 140
NUMERIC_WIDTH = 100


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


def make_result_table(parent: QWidget | None = None) -> QTableView:
    """A results table: whole-row multi-selection, read-only, fixed row heights."""
    table = QTableView(parent)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.verticalHeader().setVisible(False)
    # Fixed row heights and column widths: sizing to contents would read every row.
    table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
    table.verticalHeader().setDefaultSectionSize(table.fontMetrics().height() + 8)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(False)
    header.setMinimumSectionSize(40)
    table.setMinimumWidth(TITLE_MIN_WIDTH)
    return table


def set_column_widths(table: QTableView, columns: Sequence[ResultColumn]) -> None:
    """The title takes the remaining width; the other columns start narrow and can be
    resized. Call again whenever the columns change."""
    header = table.horizontalHeader()
    for i, column in enumerate(columns):
        if column.key == "title":
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
        else:
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            table.setColumnWidth(i, NUMERIC_WIDTH if column.numeric else COLUMN_WIDTH)
