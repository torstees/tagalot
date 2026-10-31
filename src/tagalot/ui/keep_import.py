"""Import keep…: the preview shown before a keep export changes anything (#613, DESIGN.md
§4 *Backups*).

```
Import Research keep export.yaml
14 items found, 1 not found: 9 to change; 2 new tags; 4 other records; 1 conflict.
┌──────────────────────────────────────────────────────┐
│ Conflicts: kept as they are here (1)                 │
│   Paper “X”: Year is '2001' here, '1999' in the file │
│ Items to change (9)                                  │
│   Paper “X”: tags, Year, note                        │
│ …                                                    │
└──────────────────────────────────────────────────────┘
[ ] Use the file's values where this keep's differ
                                     [Import] [Cancel]
```

Both plans (keeping this keep's values, and using the file's) are made before the dialog
opens, so the checkbox switches between them at once; it shows only when there are
conflicts.
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

from tagalot.core.keep_import import KeepImportPlan


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"1 {one}" if n == 1 else f"{n:,} {many or one + 's'}"


def summary_text(plan: KeepImportPlan) -> str:
    """One line: what the import would do."""
    found = f"{_count(plan.found, 'item')} found"
    if plan.not_found:
        found += f", {len(plan.not_found):,} not found"
    parts = [f"{found}: {len(plan.changed):,} to change"]
    tags = plan.tags
    if tags.new:
        parts.append(_count(len(tags.new), "new tag"))
    if tags.updated:
        parts.append(f"{len(tags.updated):,} tags updated")
    if plan.records:
        parts.append(_count(len(plan.records), "other record"))
    if plan.conflicts:
        parts.append(_count(len(plan.conflicts), "conflict"))
    if plan.problems:
        parts.append(_count(len(plan.problems), "problem"))
    text = "; ".join(parts) + "."
    if not plan.changes:
        text += " This keep has everything in the file already."
    return text


def report_text(plan: KeepImportPlan) -> str:
    """Every change, conflict, and problem."""
    settled = (
        "Conflicts: the file's values replace these" if plan.replace else "Conflicts: kept here"
    )
    tags = plan.tags
    sections: list[tuple[str, list[str]]] = [
        (settled, list(plan.conflicts)),
        ("Not found in this keep", list(plan.not_found)),
        ("Problems, left out", list(plan.problems)),
        ("New tags", list(tags.new)),
        ("Tags updated", [f"{u.path}: {', '.join(u.changes)}" for u in tags.updated]),
        ("Items to change", list(plan.changed)),
        ("Other records", list(plan.records)),
    ]
    lines: list[str] = []
    for title, items in sections:
        if items:
            lines.append(f"{title} ({len(items):,})")
            lines += [f"  {item}" for item in items]
            lines.append("")
    return "\n".join(lines).rstrip() or "Nothing to change."


class KeepImportDialog(QDialog):
    """Shows what importing a keep export would do; :meth:`replace` says which plan was
    chosen."""

    def __init__(
        self,
        name: str,
        keep: KeepImportPlan,
        replace: KeepImportPlan,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import keep")
        self.resize(620, 480)
        self._plans = (keep, replace)
        heading = QLabel(f"Import {name}")
        font = QFont(heading.font())
        font.setBold(True)
        heading.setFont(font)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.replace_box = QCheckBox("Use the file's values where this keep's differ")
        self.replace_box.setToolTip(
            "Values you set by hand in this keep that differ from the file's are kept unless "
            "this is ticked. Either way it is one step: Edit → Undo takes it all back."
        )
        self.replace_box.setVisible(bool(keep.conflicts))
        self.replace_box.toggled.connect(lambda _: self._update())
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
        layout.addWidget(self.replace_box)
        layout.addWidget(self.buttons)
        self._update()

    def replace(self) -> bool:
        return self.replace_box.isChecked()

    def plan(self) -> KeepImportPlan:
        return self._plans[self.replace()]

    def _update(self) -> None:
        plan = self.plan()
        self.summary.setText(summary_text(plan))
        self.report.setPlainText(report_text(plan))
        self.import_button.setEnabled(plan.changes)
        self.import_button.setToolTip(
            "" if plan.changes else "Nothing in this file would change the keep"
        )
