"""Import tags…: the preview shown before a tag file changes anything (#395, DESIGN.md §12).

```
Import tags from Tags.yaml
2 new tags, 1 updated, 1 problem.
┌──────────────────────────────────────────┐
│ New tags (2)                              │
│   Genre > Horror                          │
│ Updated (1)                               │
│   Genre > Science Fiction: color          │
│ Problems, left out (1)                    │
│   Line 7: 'blue' isn't a color…           │
└──────────────────────────────────────────┘
[ ] Delete tags not in this file that no item uses
                               [Import] [Cancel]
```

Both plans (with and without deleting) are made before the dialog opens, so the checkbox
switches between them at once.
"""

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.tag_yaml import ImportPlan


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"1 {one}" if n == 1 else f"{n:,} {many or one + 's'}"


def summary_text(plan: ImportPlan) -> str:
    """One line: what the import would do."""
    if plan.unreadable:
        return "Nothing can be imported: the file can't be read."
    parts = []
    if plan.new:
        parts.append(_count(len(plan.new), "new tag"))
    if plan.updated:
        parts.append(f"{len(plan.updated):,} updated")
    if plan.deleted:
        parts.append(f"{len(plan.deleted):,} deleted")
    if plan.problems:
        parts.append(_count(len(plan.problems), "problem"))
    if not plan.changes:
        parts.insert(0, "Nothing to change")
    return ", ".join(parts) + "."


def report_text(plan: ImportPlan) -> str:
    """Every tag the import adds, changes, or deletes, and every problem."""
    sections: list[tuple[str, list[str]]] = [
        ("Problems, left out", [str(p) for p in plan.problems]),
        ("New tags", list(plan.new)),
        ("Updated", [f"{u.path}: {', '.join(u.changes)}" for u in plan.updated]),
        ("Deleted, since no item uses them", list(plan.deleted)),
        ("Kept, since items use them", list(plan.kept)),
    ]
    lines: list[str] = []
    if plan.removed_uses:
        lines += [
            f"A changed “applies to” takes tags off {_count(plan.removed_uses, 'item')}.",
            "",
        ]
    for title, items in sections:
        if items:
            lines.append(f"{title} ({len(items):,})")
            lines += [f"  {item}" for item in items]
            lines.append("")
    return "\n".join(lines).rstrip() or "The keep's tags already match this file."


class TagImportDialog(QDialog):
    """Shows what importing a file would do; :meth:`delete_unused` says which plan was
    chosen."""

    def __init__(
        self, name: str, keep: ImportPlan, delete: ImportPlan, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import tags")
        self.resize(560, 440)
        self._plans = (keep, delete)
        heading = QLabel(f"Import tags from {name}")
        font = QFont(heading.font())
        font.setBold(True)
        heading.setFont(font)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.delete_box = QCheckBox("Delete tags not in this file that no item uses")
        self.delete_box.setToolTip(
            "A tag stays if the file names it (or one of its aliases), if any item has it, "
            "or if one of its sub-tags stays. A renamed tag that's in use stays: merge it by "
            "hand."
        )
        self.delete_box.toggled.connect(lambda _: self._update())
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.import_button = self.buttons.addButton(
            "Import", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(self.summary)
        layout.addWidget(self.report, 1)
        layout.addWidget(self.delete_box)
        layout.addWidget(self.buttons)
        self._update()

    def delete_unused(self) -> bool:
        return self.delete_box.isChecked()

    def plan(self) -> ImportPlan:
        return self._plans[self.delete_unused()]

    def _update(self) -> None:
        plan = self.plan()
        self.summary.setText(summary_text(plan))
        self.report.setPlainText(report_text(plan))
        self.import_button.setEnabled(plan.changes and not plan.unreadable)
        self.delete_box.setEnabled(not plan.unreadable)
