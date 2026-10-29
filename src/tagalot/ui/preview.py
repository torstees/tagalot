"""The preview of the selected item, under the navigation pane (DESIGN.md §12 "Select";
left of the results since #493, so they get the window's full height).

```
┌──────────────┐
│   picture    │
└──────────────┘
forest.png · Image
Aurora Studio · .png · 1600 x 900
Asset files > …/forest.png
A misty forest at dawn…
```

One selected item shows its thumbnail, title, type, card fields, primary file, and the
start of its description (#414); several show how many and list their titles (#497); none
shows a hint. The details
load in a worker. Double-click opens the page.
"""

import shiboken6
from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtGui import QMouseEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget

from tagalot.core.detail import Preview, load_preview, load_titles
from tagalot.core.session import KeepSession
from tagalot.ui.fonts import follow, place_font
from tagalot.ui.icons import icon_for
from tagalot.ui.models.results import display_value, wrapped
from tagalot.ui.thumbnails import ThumbnailLoader
from tagalot.ui.workers import run_in_pool

THUMBNAIL = 160
"""The height of the preview's picture, in pixels (it is as wide as the panel allows)."""
DESCRIPTION_LINES = 6
"""How much of the description the preview shows; its tooltip has more."""
LISTED = 25
"""How many of several selected items the preview names (#497); it says how many more."""

HINT = "Select an item to preview it; double-click to open it."


class ElidedLabel(QLabel):
    """One line of plain text, cut with an ellipsis to the label's width; the whole text
    is :attr:`full_text`."""

    def __init__(
        self,
        parent: QWidget | None = None,
        mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
    ) -> None:
        super().__init__(parent)
        self.full_text = ""
        self.mode = mode
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def set_full_text(self, text: str) -> None:
        self.full_text = text
        self._elide()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(self.full_text, self.mode, self.width()))


class PreviewStrip(QFrame):
    """Shows the selected item. Emits :attr:`open_requested` on double-click."""

    open_requested = Signal(int)

    def __init__(
        self,
        session: KeepSession,
        thumbnails: ThumbnailLoader | None = None,
        pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.thumbnails = thumbnails
        self._pool = pool
        self._generation = 0
        self.entity_id: int | None = None
        self.preview: Preview | None = None
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("preview_strip")

        self.thumbnail = QLabel()
        self.thumbnail.setFixedHeight(THUMBNAIL)
        self.thumbnail.setMinimumWidth(1)
        self.thumbnail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = QLabel(HINT)
        self._item_title = False
        # An item's title in the keep's titles font (#565); hints in the text's (#567).
        follow(
            self.title,
            lambda: place_font("titles" if self._item_title else "text", bold=True),
        )
        self.facts = QLabel()
        self.file = ElidedLabel(mode=Qt.TextElideMode.ElideMiddle)  # a path: keep its name
        self.description = QLabel()
        self.listed = QLabel()
        """The titles of several selected items, one a line (#497)."""
        for label in (self.facts, self.file):
            label.setStyleSheet("color: palette(placeholder-text);")
        for label in (self.title, self.facts, self.description, self.listed):
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        for label in (self.title, self.facts, self.file, self.description, self.listed):
            label.setTextFormat(Qt.TextFormat.PlainText)  # titles and paths are shown as is
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.thumbnail)
        layout.addSpacing(4)
        layout.addWidget(self.title)
        layout.addWidget(self.facts)
        layout.addWidget(self.file)
        layout.addWidget(self.description)
        layout.addWidget(self.listed)
        layout.addStretch(1)
        self.setToolTip("Double-click to open the item's page")
        if thumbnails is not None:
            thumbnails.ready.connect(self._thumbnail_ready)
        self.show_count(0)

    def show_count(self, count: int) -> None:
        """No item, or several, are selected: say so."""
        self._generation += 1
        self.entity_id = self.preview = None
        self._set_title(HINT if count == 0 else f"{count:,} items selected", item=False)
        self.facts.clear()
        self.file.set_full_text("")
        self._describe(None)
        self.thumbnail.clear()
        self.thumbnail.hide()  # the words go at the top
        self.listed.clear()
        self.listed.hide()

    def show_selection(self, entity_ids: list[int]) -> None:
        """Preview the one selected item, or say how many are selected."""
        if len(entity_ids) != 1:
            self.show_count(len(entity_ids))
            if len(entity_ids) > 1:
                self._list(entity_ids)
            return
        self._generation += 1
        entity_id = entity_ids[0]
        if entity_id == self.entity_id and self.preview is not None:
            return
        self.entity_id = entity_id
        self.thumbnail.show()
        generation, session = self._generation, self.session

        def job() -> Preview | None:
            with session.reader.connect() as conn:
                return load_preview(conn, session.schema, entity_id, session.own_fields)

        def done(preview: Preview | None) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._show(preview)

        run_in_pool(job, on_done=done, pool=self._pool)
        self._show_thumbnail()

    def _list(self, entity_ids: list[int]) -> None:
        """Name the first of several selected items, in a worker (#497)."""
        generation, session = self._generation, self.session
        shown = entity_ids[:LISTED]

        def job() -> list[str]:
            with session.reader.connect() as conn:
                return load_titles(conn, shown)

        def done(titles: list[str]) -> None:
            if not shiboken6.isValid(self) or generation != self._generation:
                return
            lines = [f"\u2022 {title}" for title in titles]
            more = len(entity_ids) - len(shown)
            if more:
                lines.append(f"and {more:,} more")
            self.listed.setText("\n".join(lines))
            self.listed.setVisible(bool(lines))

        run_in_pool(job, on_done=done, pool=self._pool)

    def _set_title(self, text: str, *, item: bool) -> None:
        if item != self._item_title:
            self._item_title = item
            self.title.setFont(place_font("titles" if item else "text", bold=True))
        self.title.setText(text)

    def _show(self, preview: Preview | None) -> None:
        self.preview = preview
        if preview is None:
            self._set_title("This item no longer exists", item=False)
            self.facts.clear()
            self.file.set_full_text("")
            self._describe(None)
            return
        self._set_title(f"{preview.title} · {preview.type_label}", item=True)
        facts = [display_value(f.value, f.display) for f in preview.facts]
        self.facts.setText(" · ".join(f for f in facts if f))
        self.file.set_full_text(preview.file or "")
        self.file.setToolTip(preview.file or "")
        self._describe(preview.description)

    def _describe(self, description: str | None) -> None:
        self.description.setText(" ".join(wrapped(description or "", 40, DESCRIPTION_LINES)))
        self.description.setVisible(bool(description))
        self.description.setToolTip("\n".join(wrapped(description or "", lines=12)))

    def _show_thumbnail(self) -> None:
        if self.thumbnails is None or self.entity_id is None:
            return
        loaded = self.thumbnails.get(self.entity_id)
        if loaded is None:
            self.thumbnail.clear()  # requested: _thumbnail_ready shows it
        elif loaded.image is not None:
            pixmap = QPixmap.fromImage(loaded.image)
            width = max(1, self.thumbnail.width())
            if pixmap.width() > width or pixmap.height() > THUMBNAIL:
                pixmap = pixmap.scaled(
                    width,
                    THUMBNAIL,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            self.thumbnail.setPixmap(pixmap)
        else:
            self.thumbnail.setPixmap(icon_for(loaded.icon).pixmap(THUMBNAIL // 2))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._show_thumbnail()  # as wide as the panel now is

    def _thumbnail_ready(self, entity_id: int) -> None:
        if entity_id == self.entity_id and shiboken6.isValid(self):
            self._show_thumbnail()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self.entity_id is not None:
            self.open_requested.emit(self.entity_id)
        super().mouseDoubleClickEvent(event)
