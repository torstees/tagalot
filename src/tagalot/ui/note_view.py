"""An item's note in its page's header (DESIGN.md §9 *Notes*, §12, #380).

```
┌────────┐  Aurora Studio (Reykjavík)
│ thumb  │  Artist
│        │  Painted skies and dusk light, since 2009. Most of
│        │  the work here was made for…
└────────┘  Show more · Open note
```

The body is rendered Markdown (``ui/markdown_doc.py``, #553). Collapsed, it shows only its
main text (no headings, pictures, tables, or code blocks), capped at the room beside the
picture; **Show more** shows all of it, styled, up to half the window's height, past
which it scrolls. **Show more** shows whenever the collapsed note leaves something out.
Its pictures are only those read in a worker
(:func:`~tagalot.core.note_markdown.read_pictures`): the document never loads anything
itself, so the GUI thread never touches a disk or a share. Links open in the browser, or,
relative to the note's folder (or found in an assets folder by the worker, #418), with the
file's own program.

An item without a note file has a note typed here (#383): **Add a note**, or ✎ (or
double-clicking the note) opens a box; Ctrl+Enter or clicking away saves, Esc cancels. A
note file's note is edited in its own program (**Open note**).
"""

from collections.abc import Callable
from urllib.parse import unquote

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices,
    QFocusEvent,
    QImage,
    QKeyEvent,
    QResizeEvent,
    QShowEvent,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QStyle,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.note_markdown import renderable, resolve_link
from tagalot.ui import field_editor
from tagalot.ui.markdown_doc import Hidden, collapsed, fit_pictures, render

MIN_CAP = 60
"""The note is never capped shorter than this, in pixels (about three lines)."""
EDITOR_LINES = 8
"""The note's box is this many lines tall."""
PICTURES_DELAY = 250
"""Pictures are fitted to the width this long (ms) after the width last changed."""
EXPANDED_SHARE = 0.5
"""Expanded, the note is at most this share of the window's height; past it, it scrolls."""


class _Body(QTextBrowser):
    """The rendered body: no frame, as tall as it is allowed to be; it scrolls only when
    an expanded note is taller than that."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pictures: dict[str, QImage] = {}
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenLinks(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setAutoFillBackground(False)
        self.setStyleSheet("QTextBrowser { background: transparent; }")

    def loadResource(self, kind: int, name: QUrl | str) -> object:
        """Only the pictures read for this note; nothing is loaded from disk here."""
        if kind == QTextDocument.ResourceType.ImageResource.value:
            text = name.toString() if isinstance(name, QUrl) else name
            return self.pictures.get(text) or self.pictures.get(unquote(text))
        return None


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
    resized = Signal()
    """The note took another height (Show more, Show less, a new note): the page fits
    its header again."""
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
        self.links: dict[str, str] = {}
        """Links found in an assets folder: ``{path beside the note: path there}``."""
        self.text = ""
        """The note's Markdown, as shown."""
        self.editable = False
        """Whether the note is typed here (the item has no note file)."""
        self.cap = MIN_CAP
        self.expanded = False
        self.full = QTextDocument(self.body)
        """The note, all of it, styled (shown expanded)."""
        self.short = QTextDocument(self.body)
        """Its main text only (shown collapsed)."""
        self.hidden = Hidden()
        """What the collapsed note leaves out."""
        self._window: QWidget | None = None
        self._fitting = False
        self._pictures_width = -1
        """The width the pictures were last fitted to."""
        self._pictures_timer = QTimer(self)
        self._pictures_timer.setSingleShot(True)
        self._pictures_timer.setInterval(PICTURES_DELAY)
        self._pictures_timer.timeout.connect(self._fit_pictures)
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
        links: dict[str, str] | None = None,
    ) -> None:
        """Show a note's Markdown ``body``; ``folder`` (this computer's path) resolves its
        links (``links``: those found in an assets folder instead), ``pictures`` are its
        pictures by the name written, read in a worker. ``editable``: the note is typed
        here. A note being typed stays open."""
        self.folder = folder
        self.links = links or {}
        self.text = body
        self.editable = editable and not has_file
        self.body.pictures = pictures
        self._render()
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
            self._render()
            self._show_parts()
            self.edited.emit(typed)

    def _close_box(self) -> None:
        blocked = self.box.blockSignals(True)  # hiding it moves the focus: no second save
        self.box.setVisible(False)
        self.edit_row.setVisible(False)
        self.box.blockSignals(blocked)
        self._show_parts()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._window and event.type() == QEvent.Type.Resize:
            if self.expanded:
                self._fit()
            return False
        if watched is self.body.viewport() and event.type() == QEvent.Type.MouseButtonDblClick:
            self.start_editing()
            return self.editable
        return super().eventFilter(watched, event)

    # --- showing it ---

    def _render(self) -> None:
        """Make the full and collapsed documents from the note, in the current colors."""
        old = (self.full, self.short)
        palette = self.palette()
        self.full = render(renderable(self.text), palette, self.body)
        self.short, self.hidden = collapsed(self.full, palette)
        if not self.hidden:
            self.short.deleteLater()
            self.short = self.full
        self._pictures_width = -1
        self._fit_pictures()
        self.body.setDocument(self.full if self.expanded else self.short)
        for document in old:
            if document not in (self.full, self.short):
                document.deleteLater()
        self._fit()

    def set_cap(self, cap: int) -> None:
        """How tall the body may be before **Show more**."""
        self.cap = max(MIN_CAP, cap)
        self._fit()

    def _toggle(self) -> None:
        self.expanded = not self.expanded
        self._fit()

    def limit(self) -> int:
        """The tallest the expanded note may be: half the window, or the cap if more."""
        return max(self.cap, int(self.window().height() * EXPANDED_SHARE))

    def _fit(self) -> None:
        """Size the body for the note shown. Heights are measured at the body's whole width
        (no scroll bar), so whether the scroll bar shows never changes the outcome: it
        shows only when the note is taller than the limit at that width (#553)."""
        if self._fitting:
            return
        self._fitting = True
        try:
            self._fit_now()
        finally:
            self._fitting = False

    def _fit_pictures(self) -> None:
        """Scale the pictures to the width, when it changed by more than a scroll bar's
        width. Never while the window is being resized (a timer calls it once that
        pauses), and not for a scroll bar's coming and going: a picture's height follows
        the width, which changes the page's scroll bar, which changes the width… that
        bounced Qt's layout endlessly and overflowed its stack (#553's review)."""
        width = self.body.width()
        slack = self.body.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent) + 8
        if self._pictures_width >= 0 and abs(width - self._pictures_width) <= slack:
            return
        self._pictures_width = width
        fit_pictures(self.full, max(width - slack, 1), self.body.pictures)
        if self.isVisible():
            self._fit()

    def _fit_now(self) -> None:
        width = self.body.width()
        shown = self.full if self.expanded else self.short
        if self.body.document() is not shown:
            self.body.setDocument(shown)
        short = _height_at(self.short, width)
        more = bool(self.hidden) or short > self.cap
        self.more.setVisible(more and self.body.isVisibleTo(self))
        self.more.setText("Show less" if self.expanded else "Show more")
        if self.expanded:
            full, limit = _height_at(self.full, width), self.limit()
            scrolls = full > limit
            height = limit if scrolls else full
        else:
            scrolls, height = False, min(short, self.cap)
        self.body.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn
            if scrolls
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        if not self.expanded:
            self.body.verticalScrollBar().setValue(0)
        if height != self.body.height() or self.body.minimumHeight() != height:
            self.body.setFixedHeight(height)
            self.resized.emit()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._fit()
            self._pictures_timer.start()

    def showEvent(self, event: QShowEvent) -> None:
        """Follow the window's height, which sets how tall the expanded note may be."""
        super().showEvent(event)
        window = self.window()
        if window is not self._window:
            if self._window is not None and shiboken6.isValid(self._window):
                self._window.removeEventFilter(self)
            self._window = window
            window.installEventFilter(self)
        self._fit()

    def changeEvent(self, event: QEvent) -> None:
        """New colors (the system's light or dark mode): style the note again."""
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "full"):
            self._render()

    def _follow(self, url: QUrl) -> None:
        """A link in the note: a web address in the browser, a file beside the note with
        its program."""
        text = url.toString()
        if url.scheme() in ("http", "https", "mailto"):
            field_editor.open_web_address(text)  # looked up now, so tests can stand in
            return
        if self.folder is not None and (path := resolve_link(self.folder, text)) is not None:
            self.open_file(self.links.get(path, path))


def _height_at(document: QTextDocument, width: int) -> int:
    """How tall ``document`` is at ``width``; its own width is left as it was (the body
    lays out the one it shows)."""
    before = document.textWidth()
    document.setTextWidth(width)
    height = int(document.size().height()) + 2
    document.setTextWidth(before)
    return height


def _open_local(path: str) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))
