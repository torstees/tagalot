"""The Details tab beside Tags (#468, DESIGN.md §12 "Detail page").

```
│ Tags │ Details │
│ Details ──────  │
│  Year           │
│    1957         │
│ Extra fields ─  │
│ Files ────────  │
```

On an item's page it shows that page's sections (:attr:`DetailPage.details`); on a search,
the selected item's; otherwise it says why it's empty. The panel shown belongs to its page:
when another is shown, it goes back to its page, hidden.
"""

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDockWidget, QLabel, QStackedWidget, QWidget

NOTHING_OPEN = "Open an item, or select one in a search, to see its details here."
NOTHING_SELECTED = "Select an item to see its details here."


def several_selected(count: int) -> str:
    return f"{count:,} items selected. Select one to see its details here."


class DetailsDock(QDockWidget):
    """The Details tab: one item's sections, or a message."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Details", parent)
        self.setObjectName("details_dock")
        self.stack = QStackedWidget()
        self.message = QLabel(NOTHING_OPEN)
        self.message.setObjectName("details_message")
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setMargin(12)
        self.message.setStyleSheet("color: palette(placeholder-text);")
        self.stack.addWidget(self.message)
        self.setWidget(self.stack)
        self._panel: QWidget | None = None
        self._owner: QWidget | None = None

    @property
    def panel(self) -> QWidget | None:
        """The sections shown, if any."""
        if self._panel is not None and shiboken6.isValid(self._panel):
            return self._panel if self.stack.currentWidget() is self._panel else None
        return None

    def show_panel(self, owner: QWidget, panel: QWidget) -> None:
        """Show ``panel``, an item's sections belonging to ``owner`` (its page)."""
        if self.panel is panel:
            return
        self._release()
        self.stack.addWidget(panel)
        self.stack.setCurrentWidget(panel)
        panel.show()
        self._panel, self._owner = panel, owner

    def show_message(self, text: str) -> None:
        """Show ``text`` instead of an item's sections."""
        self._release()
        self.message.setText(text)
        self.stack.setCurrentWidget(self.message)

    def _release(self) -> None:
        """Give the panel shown back to its page (hidden), or let it go with its page."""
        panel, owner = self._panel, self._owner
        self._panel = self._owner = None
        if panel is None or not shiboken6.isValid(panel):
            return
        self.stack.removeWidget(panel)
        panel.hide()
        if owner is not None and shiboken6.isValid(owner):
            panel.setParent(owner)
        else:
            panel.deleteLater()
