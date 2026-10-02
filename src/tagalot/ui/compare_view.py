"""The compare pane: items side by side, under the Dedupe page's lists (DESIGN.md §12
"Dedupe"; #119).

A column per item, headed by its name, with its thumbnail, then a row per fact
(``core.compare``): the type, the theme's fields, the user's own fields, tags, contents,
and the files' format, size, and places. Rows whose values differ are highlighted.
Double-clicking a column opens its item.
"""

from collections.abc import Callable, Sequence

import shiboken6
from PySide6.QtCore import QModelIndex, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.compare import Comparison, compare_items
from tagalot.core.session import KeepSession
from tagalot.ui.thumbnails import ThumbnailLoader, icon_for
from tagalot.ui.workers import run_in_pool

THUMBNAIL = 96
DIFFERS = QColor(255, 190, 0, 70)
"""The background of a row whose values differ (see-through: it suits light and dark)."""
EMPTY = "—"
"""Shown for an item without a value."""


class ComparePane(QWidget):
    """See the module docstring."""

    open_entity = Signal(int)
    merge_requested = Signal(list)
    """Merge… was pressed: the items, as (id, title, where), in column order."""

    def __init__(
        self,
        session: KeepSession,
        thumbnails: ThumbnailLoader | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.thumbnails = thumbnails
        self.comparison: Comparison | None = None
        self._generation = 0
        self._note = ""

        self.summary = QLabel()
        self.summary.setObjectName("compare_summary")
        self.summary.setWordWrap(True)
        self.merge_button = QPushButton("Merge\u2026")
        self.merge_button.setObjectName("merge")
        self.merge_button.clicked.connect(self._merge)
        self.merge_button.setVisible(False)
        header = QHBoxLayout()
        header.addWidget(self.summary, 1)
        header.addWidget(self.merge_button)
        self.table = QTableWidget()
        self.table.setObjectName("compare")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setWordWrap(True)
        self.table.setIconSize(QSize(THUMBNAIL, THUMBNAIL))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        self.table.doubleClicked.connect(self._activated)
        self.table.horizontalHeader().sectionDoubleClicked.connect(self._open_column)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self.table, 1)
        if thumbnails is not None:
            thumbnails.ready.connect(self._thumbnail_ready)
        self.clear("Select a group or a pair to compare its items here.")

    def clear(self, message: str) -> None:
        """Show nothing but ``message``."""
        self._generation += 1
        self.comparison = None
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.table.setVisible(False)
        self.merge_button.setVisible(False)
        self.summary.setText(message)

    def compare(self, entity_ids: Sequence[int], note: str = "") -> None:
        """Read these items, in a worker, and show them side by side, after ``note``."""
        self._generation += 1
        generation, session, ids = self._generation, self.session, list(entity_ids)
        self._note = note
        self.summary.setText(f"{note} Loading…".strip())

        def job() -> Comparison:
            with session.reader.connect() as conn:
                return compare_items(conn, session.schema, ids, known_root(session))

        def done(found: Comparison) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._show(found)

        run_in_pool(job, on_done=done)

    def _show(self, found: Comparison) -> None:
        self.comparison = found
        if not found.items:
            self.clear("These items no longer exist; press F5.")
            return
        differing = sum(r.differs for r in found.rows)
        parts = [self._note] if self._note else []
        if len(found.items) > 1:
            parts.append(
                "Highlighted rows differ." if differing else "Everything shown is the same."
            )
        if found.more:
            parts.append(f"{found.more:,} more not shown.")
        parts.append("Double-click a column to open its item.")
        self.summary.setText(" ".join(parts))
        table = self.table
        table.clear()
        table.setColumnCount(len(found.items))
        table.setRowCount(len(found.rows) + 1)
        table.setVerticalHeaderItem(0, QTableWidgetItem(""))
        for column, item in enumerate(found.items):
            title = QTableWidgetItem(item.title)
            title.setToolTip(f"{item.type_label}: {item.title}")
            table.setHorizontalHeaderItem(column, title)
            cell = QTableWidgetItem()
            cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(0, column, cell)
        for n, row in enumerate(found.rows, start=1):
            header = QTableWidgetItem(row.label)
            table.setVerticalHeaderItem(n, header)
            if row.differs:
                font = QFont(header.font())
                font.setBold(True)
                header.setFont(font)
                header.setToolTip("These differ")
            for column, value in enumerate(row.values):
                cell = QTableWidgetItem(value or EMPTY)
                cell.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                if not value:
                    cell.setForeground(self.palette().placeholderText())
                if row.differs:
                    cell.setBackground(DIFFERS)
                table.setItem(n, column, cell)
        table.setVisible(True)
        self._enable_merge(found)
        for item in found.items:
            self._show_thumbnail(item.id)

    def _enable_merge(self, found: Comparison) -> None:
        """Merge… shows for two items or more; it works on items of one type."""
        self.merge_button.setVisible(len(found.items) > 1)
        same = len({i.type_label for i in found.items}) == 1
        self.merge_button.setEnabled(same)
        self.merge_button.setToolTip(
            "Make these one item: choose which to keep, and what it takes from the others"
            if same
            else "Only items of the same type can be merged"
        )

    def _merge(self) -> None:
        found = self.comparison
        if found is None:
            return
        files = found.row("files")
        places = files.values if files is not None else ("",) * len(found.items)
        self.merge_requested.emit(
            [
                (i.id, i.title, where.split("\n")[0])
                for i, where in zip(found.items, places, strict=True)
            ]
        )

    def _show_thumbnail(self, entity_id: int) -> None:
        if self.thumbnails is None or self.comparison is None:
            return
        loaded = self.thumbnails.get(entity_id)
        if loaded is None:
            return  # requested: _thumbnail_ready shows it
        for column, item in enumerate(self.comparison.items):
            cell = self.table.item(0, column)
            if item.id != entity_id or cell is None:
                continue
            if loaded.image is not None:
                pixmap = QPixmap.fromImage(loaded.image).scaled(
                    THUMBNAIL,
                    THUMBNAIL,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                cell.setData(Qt.ItemDataRole.DecorationRole, pixmap)
            else:
                cell.setIcon(icon_for(loaded.icon))

    def _thumbnail_ready(self, entity_id: int) -> None:
        if shiboken6.isValid(self):
            self._show_thumbnail(entity_id)

    def _activated(self, index: QModelIndex) -> None:
        self._open_column(index.column())

    def _open_column(self, column: int) -> None:
        if self.comparison is not None and 0 <= column < len(self.comparison.items):
            self.open_entity.emit(self.comparison.items[column].id)


def known_root(session: KeepSession) -> Callable[[str], str | None]:
    """This machine's path for a root id, or ``None`` for a root no longer in keep.toml."""

    def path(root_id: str) -> str | None:
        try:
            return session.root_path(root_id)
        except StopIteration:
            return None

    return path
