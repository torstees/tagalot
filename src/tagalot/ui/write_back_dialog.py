"""The preview of **Write to file…** (DESIGN.md §4 "Writing back to files", #299): each
file's front matter before and after, a box to leave a file out, and why a file can't be
written. Nothing is written until the user presses **Write**."""

import difflib

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.writeback import FileWrite, WritePlan

_WRITE = Qt.ItemDataRole.UserRole


def front_matter_diff(write: FileWrite) -> str:
    """What changes in one file's front matter, as unified-diff lines (``-`` before,
    ``+`` after), or why nothing does."""
    if write.problem is not None:
        return f"Not written: {write.problem}."
    if not write.changes:
        return "Nothing to change: its front matter already says this.\n\n" + write.after
    lines = difflib.unified_diff(
        write.before.splitlines(), write.after.splitlines(), "now", "after", n=99, lineterm=""
    )
    # Without the ---/+++ header lines and the @@ line numbers.
    return "\n".join(line for line in list(lines)[2:] if not line.startswith("@@"))


class WriteBackDialog(QDialog):
    """Preview and choose the files to write; :meth:`chosen` gives the ticked ones."""

    def __init__(self, plan: WritePlan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Write to file")
        self.resize(820, 480)
        self.plan = plan
        intro = QLabel(
            "Tagalot changes only each file's front matter (its tags, and the fields you "
            "edited), and keeps a copy of each file it changes in the keep's backups folder. "
            "This can't be undone with Edit → Undo."
        )
        intro.setWordWrap(True)
        self.files = QListWidget()
        self.files.setObjectName("files")
        self.preview = QPlainTextEdit()
        self.preview.setObjectName("preview")
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)))
        for write in plan.files:
            item = QListWidgetItem(f"{write.title} — {write.relpath}")
            item.setData(_WRITE, write)
            if write.changes:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
            else:  # nothing to tick: it isn't written
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setForeground(self.palette().placeholderText())
                item.setToolTip(write.problem or "Nothing to change")
            self.files.addItem(item)
        self.files.currentItemChanged.connect(self._show)
        self.files.itemChanged.connect(lambda _: self._count())
        splitter = QSplitter()
        splitter.addWidget(self.files)
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(1, 2)
        self.skipped = QLabel(
            "Not written: " + "; ".join(f"{title} ({why})" for title, why in plan.skipped) + "."
        )
        self.skipped.setObjectName("skipped")
        self.skipped.setWordWrap(True)
        self.skipped.setVisible(bool(plan.skipped))
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.skipped)
        layout.addWidget(self.buttons)
        if self.files.count():
            self.files.setCurrentRow(0)
        self._count()

    def chosen(self) -> list[FileWrite]:
        """The ticked files (only files that change can be ticked)."""
        found = []
        for row in range(self.files.count()):
            item = self.files.item(row)
            write = item.data(_WRITE)
            if write.changes and item.checkState() == Qt.CheckState.Checked:
                found.append(write)
        return found

    def _show(self, item: QListWidgetItem | None) -> None:
        self.preview.setPlainText(front_matter_diff(item.data(_WRITE)) if item else "")

    def _count(self) -> None:
        count = len(self.chosen())
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText(f"Write {count} file" + ("" if count == 1 else "s"))
        ok.setEnabled(count > 0)
