"""Choosing which item types a tag applies to (#135, DESIGN.md §7 "Tag types").

```
Apply 'Genre' to:
 (•) Every type
 ( ) Only these:  ☑ Books  ☑ Comics  ☐ Authors  ☐ Series
                  (Superhero allows only Comics, from its own setting)
```

A tag's types are inherited by its sub-tags, which can only narrow them, so a sub-tag's
dialog offers only its parent's types.
"""

from collections.abc import Mapping

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)


class TagTypesDialog(QDialog):
    """Every type, or only the ticked ones; :meth:`chosen` is the answer."""

    def __init__(
        self,
        name: str,
        plurals: Mapping[str, str],
        own: frozenset[str] | None,
        inherited: frozenset[str] | None,
        parent: QWidget | None = None,
    ) -> None:
        """``own`` is the tag's own setting (``None``: every type); ``inherited`` is what its
        parent allows (``None``: every type), the only types offered."""
        super().__init__(parent)
        self.setWindowTitle("Applies to")
        offered = {t: n for t, n in plurals.items() if inherited is None or t in inherited}
        self.every = QRadioButton(
            "Every type" if inherited is None else "Every type its parent allows"
        )
        self.only = QRadioButton("Only these:")
        group = QButtonGroup(self)
        group.addButton(self.every)
        group.addButton(self.only)
        self.boxes: dict[str, QCheckBox] = {}
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Apply {name!r} to:"))
        layout.addWidget(self.every)
        layout.addWidget(self.only)
        for type_id, plural in sorted(offered.items(), key=lambda kv: kv[1].casefold()):
            box = QCheckBox(plural)
            box.setChecked(own is not None and type_id in own)
            box.toggled.connect(lambda _on: self._update())
            box.setContentsMargins(24, 0, 0, 0)
            layout.addWidget(box)
            self.boxes[type_id] = box
        self.every.setChecked(own is None)
        self.only.setChecked(own is not None)
        self.every.toggled.connect(lambda _on: self._update())
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._update()

    def chosen(self) -> frozenset[str] | None:
        """The ticked types, or ``None`` for every type."""
        if self.every.isChecked():
            return None
        return frozenset(t for t, box in self.boxes.items() if box.isChecked())

    def _update(self) -> None:
        for box in self.boxes.values():
            box.setEnabled(self.only.isChecked())
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(self.every.isChecked() or any(b.isChecked() for b in self.boxes.values()))
