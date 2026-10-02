"""Merge…: choose which item to keep, see what moves to it, and settle the values the
user set differently by hand (DESIGN.md §13; #120).

The plan (``KeepSession.plan_merge``) is read in a worker each time the kept item changes.
The window does the merging (``KeepSession.merge_items``), as one undo step.
"""

from collections.abc import Sequence
from functools import partial

import shiboken6
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.merge import MergeError, MergePlan
from tagalot.core.session import KeepSession
from tagalot.ui.workers import run_in_pool


class MergeDialog(QDialog):
    """See the module docstring. ``items`` are (id, title, where) in the compare pane's
    order; the first is kept unless the user picks another."""

    def __init__(
        self,
        session: KeepSession,
        items: Sequence[tuple[int, str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.items = list(items)
        self.plan: MergePlan | None = None
        self.choices: dict[str, int] = {}
        self._generation = 0
        self.setWindowTitle(f"Merge {len(self.items)} items")
        self.resize(520, 420)

        keep_box = QGroupBox("Keep")
        keep_layout = QVBoxLayout(keep_box)
        self.keep_group = QButtonGroup(self)
        for n, (entity_id, title, where) in enumerate(self.items):
            button = QRadioButton(f"{title}  —  {where}" if where else title)
            button.setObjectName(f"keep_{entity_id}")
            button.setChecked(n == 0)
            self.keep_group.addButton(button, entity_id)
            keep_layout.addWidget(button)
        self.keep_group.idToggled.connect(self._keep_toggled)

        self.summary = QLabel()
        self.summary.setObjectName("merge_summary")
        self.summary.setWordWrap(True)
        self.conflicts_box = QGroupBox("Your values differ: choose which to keep")
        self.conflicts_layout = QVBoxLayout(self.conflicts_box)
        self.conflicts_box.setVisible(False)
        self.unplaced = QLabel()
        self.unplaced.setObjectName("merge_unplaced")
        self.unplaced.setWordWrap(True)
        self.unplaced.setVisible(False)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.merge_button = self.buttons.addButton("Merge", QDialogButtonBox.ButtonRole.AcceptRole)
        self.merge_button.setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(keep_box)
        layout.addWidget(self.summary)
        layout.addWidget(self.conflicts_box)
        layout.addWidget(self.unplaced)
        layout.addStretch(1)
        layout.addWidget(self.buttons)
        self.load()

    @property
    def keep_id(self) -> int:
        return self.keep_group.checkedId()

    @property
    def other_ids(self) -> list[int]:
        return [i for i, _, _ in self.items if i != self.keep_id]

    def _keep_toggled(self, _entity_id: int, on: bool) -> None:
        if on:
            self.load()

    def _choose(self, key: str, entity_id: int, on: bool) -> None:
        if on:
            self.choices[key] = entity_id

    def load(self) -> None:
        """Read what merging into the chosen item would do, in a worker."""
        self._generation += 1
        generation, session = self._generation, self.session
        keep, others = self.keep_id, self.other_ids
        self.merge_button.setEnabled(False)
        self.summary.setText("Reading the items…")

        def job() -> MergePlan | str:
            try:
                return session.plan_merge(keep, others)
            except MergeError as error:
                return str(error)

        def done(plan: MergePlan | str) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._show(plan)

        run_in_pool(job, on_done=done)

    def _show(self, plan: MergePlan | str) -> None:
        if isinstance(plan, str):
            self.plan = None
            self.summary.setText(plan)
            self.conflicts_box.setVisible(False)
            self.unplaced.setVisible(False)
            return
        self.plan = plan
        moves = [
            _count(plan.tags, "tag"),
            _count(plan.files, "file"),
            _count(plan.containers, "container"),
            _count(plan.contents, "contained item"),
            _count(plan.related, "relationship"),
        ]
        moving = [m for m in moves if m]
        others = ", ".join(repr(t) for _, t in plan.others)
        text = (
            f"{plan.keep[1]!r} gains {_join(moving)} from {others}."
            if moving
            else f"{plan.keep[1]!r} gains nothing new from {others}."
        )
        text += (
            " The others are then gone; their files stay where they are, and scans won't"
            " make them again. Edit → Undo brings them back."
        )
        self.summary.setText(text)
        self._show_conflicts(plan)
        if plan.unplaced:
            one = len(plan.unplaced) == 1
            self.unplaced.setText(
                f"{plan.keep[1]!r} holds just one file where "
                + ("this one" if one else "these")
                + " would go, so "
                + ("it" if one else "they")
                + " will be linked to no item (Triage lists "
                + ("it" if one else "them")
                + " under Unlinked files):\n"
                + "\n".join(f"• {where}" for where in plan.unplaced)
            )
        self.unplaced.setVisible(bool(plan.unplaced))
        self.merge_button.setEnabled(True)

    def _show_conflicts(self, plan: MergePlan) -> None:
        while self.conflicts_layout.count():
            item = self.conflicts_layout.takeAt(0)
            if item is not None and (widget := item.widget()) is not None:
                widget.deleteLater()
        titles = {i: t for i, t, _ in self.items}
        self.choices = {c.key: c.default for c in plan.conflicts}
        for conflict in plan.conflicts:
            row = QWidget()
            row.setObjectName(f"conflict_{conflict.key}")
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.addWidget(QLabel(f"{conflict.label}:"))
            group = QButtonGroup(row)
            for choice in conflict.choices:
                button = QRadioButton(choice.text or "(empty)")
                button.setToolTip(f"From {titles.get(choice.entity_id, choice.entity_id)}")
                button.setChecked(choice.entity_id == conflict.default)
                group.addButton(button, choice.entity_id)
                line.addWidget(button)
            line.addStretch(1)
            group.idToggled.connect(partial(self._choose, conflict.key))
            self.conflicts_layout.addWidget(row)
        self.conflicts_box.setVisible(bool(plan.conflicts))


def _count(n: int, noun: str) -> str:
    return "" if not n else f"1 {noun}" if n == 1 else f"{n:,} {noun}s"


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
