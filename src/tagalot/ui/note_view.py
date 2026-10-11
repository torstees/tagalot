"""An item's note in its page's header (DESIGN.md §9 *Notes*, §12, #380).

```
┌────────┐  Aurora Studio (Reykjavík)
│ thumb  │  Artist
│        │  Painted skies and dusk light, since 2009. Most of
│        │  the work here was made for…
└────────┘  Show more · Open note
```

The body is rendered Markdown, capped at the room beside the picture; **Show more**
expands it in place. Its pictures are only those read in a worker
(:func:`~tagalot.core.note_markdown.read_pictures`): the document never loads anything
itself, so the GUI thread never touches a disk or a share. Links open in the browser, or,
relative to the note's folder, with the file's own program.
"""

from collections.abc import Callable
from urllib.parse import unquote

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QResizeEvent, QTextDocument
from PySide6.QtWidgets import QFrame, QHBoxLayout, QTextBrowser, QToolButton, QVBoxLayout, QWidget

from tagalot.core.note_markdown import renderable, resolve_link
from tagalot.ui import field_editor

MIN_CAP = 60
"""The note is never capped shorter than this, in pixels (about three lines)."""


class _Body(QTextBrowser):
    """The rendered body: no frame or scroll bars, as tall as it is allowed to be."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pictures: dict[str, QImage] = {}
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenLinks(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setAutoFillBackground(False)
        self.setStyleSheet("QTextBrowser { background: transparent; }")
        self.document().setDocumentMargin(0)

    def loadResource(self, kind: int, name: QUrl | str) -> object:
        """Only the pictures read for this note; nothing is loaded from disk here."""
        if kind == QTextDocument.ResourceType.ImageResource.value:
            text = name.toString() if isinstance(name, QUrl) else name
            return self.pictures.get(text) or self.pictures.get(unquote(text))
        return None

    def content_height(self) -> int:
        document = self.document()
        document.setTextWidth(self.viewport().width())
        return int(document.size().height()) + 2


class NoteView(QWidget):
    """An item's note: its body, **Show more**, and **Open note**."""

    open_note = Signal()
    """Open the note's file with its program."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("note_view")
        self.body = _Body()
        self.body.setObjectName("note_body")
        self.body.anchorClicked.connect(self._follow)
        self.more = QToolButton()
        self.more.setObjectName("note_more")
        self.more.setAutoRaise(True)
        self.more.clicked.connect(self._toggle)
        self.open_button = QToolButton()
        self.open_button.setObjectName("open_note")
        self.open_button.setText("Open note")
        self.open_button.setAutoRaise(True)
        self.open_button.setToolTip("Edit the note in its own program")
        self.open_button.clicked.connect(self.open_note)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addWidget(self.more)
        footer.addWidget(self.open_button)
        footer.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.body)
        layout.addLayout(footer)
        self.folder: str | None = None
        self.cap = MIN_CAP
        self.expanded = False
        self.open_file: Callable[[str], None] = _open_local
        """Opens a file a link names (tests stand in for the system)."""
        self.setVisible(False)

    def show_note(
        self, body: str, folder: str | None, pictures: dict[str, QImage], has_file: bool
    ) -> None:
        """Show a note's Markdown ``body``; ``folder`` (this computer's path) resolves its
        links, ``pictures`` are its pictures by the name written, read in a worker."""
        self.folder = folder
        self.body.pictures = pictures
        self.body.setMarkdown(renderable(body))
        self.body.setVisible(bool(body.strip()))
        self.open_button.setVisible(has_file)
        self.setVisible(bool(body.strip()) or has_file)
        self._fit()

    def set_cap(self, cap: int) -> None:
        """How tall the body may be before **Show more**."""
        self.cap = max(MIN_CAP, cap)
        self._fit()

    def _toggle(self) -> None:
        self.expanded = not self.expanded
        self._fit()

    def _fit(self) -> None:
        full = self.body.content_height()
        longer = full > self.cap
        self.more.setVisible(longer and self.body.isVisibleTo(self))
        self.more.setText("Show less" if self.expanded else "Show more")
        self.body.setFixedHeight(full if self.expanded or not longer else self.cap)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit()

    def _follow(self, url: QUrl) -> None:
        """A link in the note: a web address in the browser, a file beside the note with
        its program."""
        text = url.toString()
        if url.scheme() in ("http", "https", "mailto"):
            field_editor.open_web_address(text)  # looked up now, so tests can stand in
            return
        if self.folder is not None and (path := resolve_link(self.folder, text)) is not None:
            self.open_file(path)


def _open_local(path: str) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))
