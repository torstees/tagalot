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

An item without a note file has a note typed here (#383): **Add a note**, or ✎ (or
double-clicking the note) opens a box; Ctrl+Enter or clicking away saves, Esc cancels. A
note file's note is edited in its own program (**Open note**).
"""

from collections.abc import Callable
from urllib.parse import unquote

from PySide6.QtCore import QEvent, QObject, Qt, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices,
    QFocusEvent,
    QImage,
    QKeyEvent,
    QResizeEvent,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.note_markdown import renderable, resolve_link
from tagalot.ui import field_editor

MIN_CAP = 60
"""The note is never capped shorter than this, in pixels (about three lines)."""
EDITOR_LINES = 8
"""The note's box is this many lines tall."""


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


class _NoteBox(QPlainTextEdit):
    """The box a note is typed in: Ctrl+Enter saves, Esc cancels, clicking away saves."""

    submitted = Signal()
    cancelled = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if enter and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.submitted.emit()
        elif event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self.submitted.emit()


class NoteView(QWidget):
    """An item's note: its body, **Show more**, and **Open note**; or, for an item without
    a note file, the note typed in Tagalot, edited in place."""

    open_note = Signal()
    """Open the note's file with its program."""
    edited = Signal(str)
    """The user typed a note (empty: removed it)."""

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
        self.add_button = QToolButton()
        self.add_button.setObjectName("add_note")
        self.add_button.setText("Add a note")
        self.add_button.setAutoRaise(True)
        self.add_button.setToolTip("Type a note about this item; it's searchable")
        self.add_button.clicked.connect(self.start_editing)
        self.pencil = QToolButton()
        self.pencil.setObjectName("edit_note")
        self.pencil.setText(field_editor.PENCIL)
        self.pencil.setAutoRaise(True)
        self.pencil.setToolTip("Edit the note (or double-click it)")
        self.pencil.clicked.connect(self.start_editing)
        self.box = _NoteBox()
        self.box.setObjectName("note_box")
        self.box.setPlaceholderText("A note about this item, in Markdown")
        self.box.submitted.connect(self._submit)
        self.box.cancelled.connect(self.cancel)
        self.box.setFixedHeight(self.box.fontMetrics().lineSpacing() * EDITOR_LINES + 12)
        self.box.setVisible(False)
        save = QToolButton()
        save.setObjectName("save_note")
        save.setText("Save")
        save.setAutoRaise(True)
        cancel = QToolButton()
        cancel.setObjectName("cancel_note")
        cancel.setText("Cancel")
        cancel.setAutoRaise(True)
        for button in (save, cancel):  # clicking one leaves the box's focus alone
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        save.clicked.connect(self._submit)
        cancel.clicked.connect(self.cancel)
        hint = QLabel("Ctrl+Enter saves, Esc cancels")
        hint.setStyleSheet("color: palette(placeholder-text);")
        self.edit_row = QWidget()
        edit_row = QHBoxLayout(self.edit_row)
        edit_row.setContentsMargins(0, 0, 0, 0)
        edit_row.addWidget(save)
        edit_row.addWidget(cancel)
        edit_row.addWidget(hint)
        edit_row.addStretch(1)
        self.edit_row.setVisible(False)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addWidget(self.more)
        footer.addWidget(self.open_button)
        footer.addWidget(self.add_button)
        footer.addWidget(self.pencil)
        footer.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.body)
        layout.addWidget(self.box)
        layout.addWidget(self.edit_row)
        layout.addLayout(footer)
        self.folder: str | None = None
        self.text = ""
        """The note's Markdown, as shown."""
        self.editable = False
        """Whether the note is typed here (the item has no note file)."""
        self.cap = MIN_CAP
        self.expanded = False
        self.open_file: Callable[[str], None] = _open_local
        """Opens a file a link names (tests stand in for the system)."""
        self.body.viewport().installEventFilter(self)
        self.setVisible(False)

    def show_note(
        self,
        body: str,
        folder: str | None,
        pictures: dict[str, QImage],
        has_file: bool,
        editable: bool = False,
    ) -> None:
        """Show a note's Markdown ``body``; ``folder`` (this computer's path) resolves its
        links, ``pictures`` are its pictures by the name written, read in a worker.
        ``editable``: the note is typed here. A note being typed stays open."""
        self.folder = folder
        self.text = body
        self.editable = editable and not has_file
        self.body.pictures = pictures
        self.body.setMarkdown(renderable(body))
        self.open_button.setVisible(has_file)
        if self.editing() and not self.editable:
            self._close_box()
        self._show_parts()

    def _show_parts(self) -> None:
        editing = self.editing()
        has_text = bool(self.text.strip())
        self.body.setVisible(has_text and not editing)
        self.add_button.setVisible(self.editable and not has_text and not editing)
        self.pencil.setVisible(self.editable and has_text and not editing)
        self.setVisible(has_text or self.editable or self.open_button.isVisibleTo(self))
        self._fit()

    # --- typing a note (#383) ---

    def editing(self) -> bool:
        """Whether the note's box is showing."""
        return self.box.isVisibleTo(self)

    def start_editing(self) -> None:
        if not self.editable:
            return
        self.box.setPlainText(self.text)
        self.box.setVisible(True)
        self.edit_row.setVisible(True)
        self._show_parts()
        self.box.setFocus(Qt.FocusReason.OtherFocusReason)
        self.box.moveCursor(QTextCursor.MoveOperation.End)

    def cancel(self) -> None:
        self._close_box()

    def _submit(self) -> None:
        if not self.editing():
            return
        typed = self.box.toPlainText()
        self._close_box()
        if typed.strip() != self.text.strip():
            self.text = typed  # shown at once; the page reloads it once saved
            self.body.setMarkdown(renderable(typed))
            self._show_parts()
            self.edited.emit(typed)

    def _close_box(self) -> None:
        blocked = self.box.blockSignals(True)  # hiding it moves the focus: no second save
        self.box.setVisible(False)
        self.edit_row.setVisible(False)
        self.box.blockSignals(blocked)
        self._show_parts()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.body.viewport() and event.type() == QEvent.Type.MouseButtonDblClick:
            self.start_editing()
            return self.editable
        return super().eventFilter(watched, event)

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
