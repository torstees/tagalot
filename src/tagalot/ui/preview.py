"""The preview strip under a search's results (DESIGN.md §12 "Select").

```
┌─────┐ forest.png · Image
│thumb│ Aurora Studio · .png · 1600 x 900
└─────┘ Asset files > Aurora Studio/Backgrounds/forest.png
```

One selected item shows its thumbnail, title, type, card fields, and primary file; several
show how many; none shows a hint. The details load in a worker. Double-click opens the page.
"""

import shiboken6
from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtGui import QFont, QMouseEvent, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from tagalot.core.detail import Preview, load_preview
from tagalot.core.session import KeepSession
from tagalot.ui.models.results import display_value
from tagalot.ui.thumbnails import ThumbnailLoader, icon_for
from tagalot.ui.workers import run_in_pool

THUMBNAIL = 96
"""The strip's thumbnail box, in pixels."""

HINT = "Select an item to preview it; double-click to open it."


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
        self.thumbnail.setFixedSize(THUMBNAIL, THUMBNAIL)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = QLabel(HINT)
        font = QFont(self.title.font())
        font.setBold(True)
        self.title.setFont(font)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.facts = QLabel()
        self.file = QLabel()
        for label in (self.facts, self.file):
            label.setStyleSheet("color: palette(placeholder-text);")
        for label in (self.title, self.facts, self.file):
            label.setTextFormat(Qt.TextFormat.PlainText)  # titles and paths are shown as is
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text = QVBoxLayout()
        text.addWidget(self.title)
        text.addWidget(self.facts)
        text.addWidget(self.file)
        text.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.thumbnail)
        layout.addSpacing(8)
        layout.addLayout(text, 1)
        self.setFixedHeight(THUMBNAIL + 14)
        self.setToolTip("Double-click to open the item's page")
        if thumbnails is not None:
            thumbnails.ready.connect(self._thumbnail_ready)
        self.show_count(0)

    def show_count(self, count: int) -> None:
        """No item, or several, are selected: say so."""
        self._generation += 1
        self.entity_id = self.preview = None
        self.title.setText(HINT if count == 0 else f"{count:,} items selected")
        self.facts.clear()
        self.file.clear()
        self.thumbnail.clear()

    def show_selection(self, entity_ids: list[int]) -> None:
        """Preview the one selected item, or say how many are selected."""
        if len(entity_ids) != 1:
            self.show_count(len(entity_ids))
            return
        self._generation += 1
        entity_id = entity_ids[0]
        if entity_id == self.entity_id and self.preview is not None:
            return
        self.entity_id = entity_id
        generation, session = self._generation, self.session

        def job() -> Preview | None:
            with session.reader.connect() as conn:
                return load_preview(conn, session.schema, entity_id)

        def done(preview: Preview | None) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._show(preview)

        run_in_pool(job, on_done=done, pool=self._pool)
        self._show_thumbnail()

    def _show(self, preview: Preview | None) -> None:
        self.preview = preview
        if preview is None:
            self.title.setText("This item no longer exists")
            self.facts.clear()
            self.file.clear()
            return
        self.title.setText(f"{preview.title} · {preview.type_label}")
        facts = [display_value(f.value, f.display) for f in preview.facts]
        self.facts.setText(" · ".join(f for f in facts if f))
        self.file.setText(preview.file or "")
        self.file.setToolTip(preview.file or "")

    def _show_thumbnail(self) -> None:
        if self.thumbnails is None or self.entity_id is None:
            return
        loaded = self.thumbnails.get(self.entity_id)
        if loaded is None:
            self.thumbnail.clear()  # requested: _thumbnail_ready shows it
        elif loaded.image is not None:
            pixmap = QPixmap.fromImage(loaded.image)
            if pixmap.width() > THUMBNAIL or pixmap.height() > THUMBNAIL:
                pixmap = pixmap.scaled(
                    THUMBNAIL,
                    THUMBNAIL,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            self.thumbnail.setPixmap(pixmap)
        else:
            self.thumbnail.setPixmap(icon_for(loaded.icon).pixmap(THUMBNAIL // 2))

    def _thumbnail_ready(self, entity_id: int) -> None:
        if entity_id == self.entity_id and shiboken6.isValid(self):
            self._show_thumbnail()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self.entity_id is not None:
            self.open_requested.emit(self.entity_id)
        super().mouseDoubleClickEvent(event)
