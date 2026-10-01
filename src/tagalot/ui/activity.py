"""The activity panel (DESIGN.md §12 "Activity panel").

A dock under the results, hidden until asked for: **View → Activity** (Ctrl+Shift+A), or
the problem badge on the status bar ("⚠ 3"). It shows:

- **Now:** the scan in progress (its latest step), or the last scan's summary.
- **Thumbnails:** how many the background queue has left (§6 step 7).
- **Folders:** each root's state (online, offline with why, not watched).
- **Problems:** the session's :class:`~tagalot.core.activity.ProblemLog`, newest first:
  when, what, where (folder > path), and the message, with **Show in file manager**,
  **Copy** (as text, for a bug report), and **Clear**.

The window feeds it; the problem list is read from the log when it changes.
"""

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.activity import KIND_LABELS, Problem
from tagalot.core.keep import RootConfig
from tagalot.core.root_admin import RootStatus
from tagalot.core.roots import local_path

ModelIndex = QModelIndex | QPersistentModelIndex


@dataclass(frozen=True)
class ShownProblem:
    problem: Problem
    where: str
    """Folder > path, or this computer's path."""
    path: str | None
    """This computer's path, if it can be worked out (for Show in file manager)."""


class ProblemsModel(QAbstractTableModel):
    """Problems, newest first: When, What, Where, Message."""

    HEADERS = ("When", "What", "Where", "Message")

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[ShownProblem] = []

    def set_rows(self, rows: list[ShownProblem]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{row.where}\n{row.problem.message}"
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return _cells(row)[index.column()]


def _cells(row: ShownProblem) -> tuple[str, str, str, str]:
    problem = row.problem
    return (
        f"{problem.when.astimezone():%H:%M:%S}",
        KIND_LABELS.get(problem.kind, problem.kind),
        row.where,
        problem.message,
    )


class ActivityPanel(QDockWidget):
    """See the module docstring. ``roots`` and ``root_path`` give the keep's folders;
    ``reveal`` shows a path in the file manager."""

    def __init__(
        self,
        roots: Callable[[], list[RootConfig]],
        root_path: Callable[[str], str | None],
        reveal: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Activity", parent)
        self.setObjectName("activity")
        self._roots = roots
        self._root_path = root_path
        self._reveal = reveal
        self.now = QLabel("No scan yet in this session.")
        self.now.setObjectName("now")
        self.now.setWordWrap(True)
        self.thumbnails = QLabel()
        self.thumbnails.setObjectName("thumbnails")
        self.thumbnails.setVisible(False)
        self.folders = QLabel()
        self.folders.setObjectName("folders")
        self.folders.setTextFormat(Qt.TextFormat.RichText)
        self.folders.setWordWrap(True)

        self.heading = QLabel()
        self.reveal_button = QPushButton("Show in file manager")
        self.reveal_button.clicked.connect(self._reveal_selected)
        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip("Copy every problem as text (for a bug report, say)")
        self.copy_button.clicked.connect(self.copy)
        self.clear_button = QPushButton("Clear")
        heading = QHBoxLayout()
        heading.addWidget(self.heading, 1)
        heading.addWidget(self.reveal_button)
        heading.addWidget(self.copy_button)
        heading.addWidget(self.clear_button)
        self.problems = ProblemsModel()
        self.table = QTableView()
        self.table.setObjectName("problems")
        self.table.setModel(self.problems)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(lambda _: self._reveal_selected())
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._enable())

        body = QWidget()
        column = QVBoxLayout(body)
        column.addWidget(self.now)
        column.addWidget(self.thumbnails)
        column.addWidget(self.folders)
        column.addLayout(heading)
        column.addWidget(self.table, 1)
        self.setWidget(body)
        self.show_problems([])

    # --- what's happening ---

    def set_now(self, text: str) -> None:
        self.now.setText(text)

    def set_queue(self, left: int) -> None:
        self.thumbnails.setText(f"Making thumbnails: {left:,} left")
        self.thumbnails.setVisible(left > 0)

    def show_folders(self, statuses: dict[str, RootStatus]) -> None:
        parts = []
        for root in self._roots():
            status = statuses.get(root.id)
            if not root.watched:
                state = "not watched"
            elif status is None or status.last_scan_at is None:
                state = "not scanned yet"
            elif status.online:
                state = "online"
            else:
                why = f": {status.last_error}" if status.last_error else ""
                state = f'<span style="color:#c0392b">offline{_escape(why)}</span>'
            parts.append(f"<b>{_escape(root.name)}</b> {state}")
        self.folders.setText("Folders: " + (" · ".join(parts) or "none"))

    # --- problems ---

    def show_problems(self, problems: list[Problem]) -> None:
        rows = [self._shown(p) for p in reversed(problems)]
        self.problems.set_rows(rows)
        self.heading.setText(
            f"<b>Problems ({len(rows):,})</b>" if rows else "<b>Problems</b>: none"
        )
        self.copy_button.setEnabled(bool(rows))
        self.clear_button.setEnabled(bool(rows))
        self._enable()

    def _shown(self, problem: Problem) -> ShownProblem:
        names = {r.id: r.name for r in self._roots()}
        if problem.path is not None:
            return ShownProblem(problem, problem.path, problem.path)
        if problem.root_id is None:
            return ShownProblem(problem, "", None)
        name = names.get(problem.root_id, problem.root_id)
        if problem.relpath is None:
            return ShownProblem(problem, name, None)
        base = self._root_path(problem.root_id)
        path = local_path(base, problem.relpath) if base is not None else None
        return ShownProblem(problem, f"{name} \u203a {problem.relpath}", path)

    def selected(self) -> ShownProblem | None:
        rows = self.table.selectionModel().selectedRows()
        return self.problems.rows[rows[0].row()] if rows else None

    def _enable(self) -> None:
        row = self.selected()
        self.reveal_button.setEnabled(row is not None and row.path is not None)

    def _reveal_selected(self) -> None:
        row = self.selected()
        if row is not None and row.path is not None:
            self._reveal(row.path)

    def copy(self) -> None:
        """Every problem as tab-separated text, newest first."""
        lines = ["\t".join(_cells(row)) for row in self.problems.rows]
        QApplication.clipboard().setText("\n".join(lines))


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
