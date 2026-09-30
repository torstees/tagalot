"""An entity's detail page (DESIGN.md §12 "Detail page").

```
┌────────┐  Blue Train
│ thumb  │  Album
└────────┘
Details ─────────────────────
  Year      1957
Folder ──────────────────────
  Jazz/Coltrane/Blue Train   NAS music
Cover ───────────────────────
  [img] [img]
Contents                 5 items
  [filter bar]
  [results: list or grid]
```

A header with the thumbnail and title, then the theme's sections top to bottom in a scroll
area. A container's contents are a search of their own below it (``within`` the entity),
with a filter bar and the usual layouts; a splitter divides the page between them.
Everything is read in a worker (:func:`~tagalot.core.detail.load_detail`); related entities
are links that open their own pages.
"""

import html
from collections.abc import Callable

import shiboken6
from PySide6.QtCore import QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QFont, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.detail import DetailSection, EntityDetail, FileRow, load_detail
from tagalot.core.ingest import TITLE
from tagalot.core.models import ResourceStatus
from tagalot.core.search_fields import contained_types
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.ui.field_editor import EditableValue
from tagalot.ui.search_view import SearchPage
from tagalot.ui.thumbnails import ThumbnailLoader, icon_for
from tagalot.ui.workers import run_in_pool

HEADER_THUMBNAIL = 192
"""The header thumbnail's box, in pixels."""

GALLERY_THUMBNAIL = 128

CRUMB = "\u203a"
"""Between breadcrumbs."""


class DetailPage(QWidget):
    """The detail page of one entity. Emits :attr:`open_entity` with an entity id when a
    related entity's link is clicked."""

    open_entity = Signal(int)
    field_edited = Signal(int, str, object)
    """The user set a field (or ``"title"``) by hand: entity id, field name, new value."""
    show_in_search = Signal(int)
    """The user asked to see this entity's contents in Search all (a Within chip)."""
    selection_changed = Signal()
    """The selected contents changed (what tagging applies to)."""
    loaded = Signal()
    """The page's content arrived (or was refreshed)."""

    def __init__(
        self,
        session: KeepSession,
        entity_id: int,
        *,
        thumbnails: ThumbnailLoader | None = None,
        make_contents: Callable[[str, SearchSpec], SearchPage] | None = None,
        pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.entity_id = entity_id
        self.thumbnails = thumbnails
        self._make_contents = make_contents
        self.contents: SearchPage | None = None
        """The search of a container's contents, once the page knows it is one."""
        self._pool = pool
        self._generation = 0
        self.detail: EntityDetail | None = None

        self.thumbnail = QLabel()
        self.thumbnail.setFixedSize(HEADER_THUMBNAIL, HEADER_THUMBNAIL)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # The title is editable in place, like the fields.
        self.title_value = EditableValue("Loading…", str, label="the name")
        self.title_value.committed.connect(
            lambda value: self.field_edited.emit(self.entity_id, TITLE, value)
        )
        self.title = self.title_value.label
        font = QFont(self.title.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 5)
        self.title.setFont(font)
        self.title_value.box.setFont(font)
        self.type_label = QLabel()
        self.type_label.setStyleSheet("color: palette(placeholder-text);")
        self.breadcrumbs = QLabel()
        self.breadcrumbs.setTextFormat(Qt.TextFormat.RichText)
        self.breadcrumbs.setWordWrap(True)
        self.breadcrumbs.linkActivated.connect(lambda href: self.open_entity.emit(int(href)))
        self.breadcrumbs.setVisible(False)
        titles = QVBoxLayout()
        titles.addWidget(self.breadcrumbs)
        titles.addWidget(self.title_value)
        titles.addWidget(self.type_label)
        titles.addStretch(1)
        header = QHBoxLayout()
        header.addWidget(self.thumbnail, 0, Qt.AlignmentFlag.AlignTop)
        header.addSpacing(12)
        header.addLayout(titles, 1)

        self._sections = QVBoxLayout()
        self._sections.setSpacing(14)
        content = QWidget()
        column = QVBoxLayout(content)
        column.addLayout(header)
        column.addSpacing(8)
        column.addLayout(self._sections)
        column.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(scroll)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.splitter)

        if thumbnails is not None:
            thumbnails.ready.connect(self._thumbnail_ready)
        self.refresh()

    # --- loading ---

    def refresh(self) -> None:
        """Read the entity again (after a scan or an edit)."""
        self._generation += 1
        generation, session, entity_id = self._generation, self.session, self.entity_id

        def job() -> EntityDetail | None:
            with session.reader.connect() as conn:
                return load_detail(conn, session.schema, entity_id, _root_path(session))

        def done(detail: EntityDetail | None) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._show(detail)

        run_in_pool(job, on_done=done, pool=self._pool)

    def _show(self, detail: EntityDetail | None) -> None:
        self.detail = detail
        while self._sections.count():
            item = self._sections.takeAt(0)
            if item is not None and (widget := item.widget()) is not None:
                widget.deleteLater()
        if detail is None:
            self.title_value.editable = False
            self.title_value.show_value("This item no longer exists", False)
            self.type_label.setText("")
            self.thumbnail.clear()
        else:
            self.title_value.editable = True
            self.title_value.field_label = detail.title_label.lower()
            self.title_value.show_value(detail.title, detail.title_edited)
            self.type_label.setText(detail.type_label)
            self._show_breadcrumbs(detail)
            for section in detail.sections:
                if section.kind == "contents" and self._show_contents(detail):
                    continue
                widget = self._section_widget(section)
                if widget is not None:
                    self._sections.addWidget(widget)
            self._show_thumbnail()
        self.loaded.emit()

    def _show_contents(self, detail: EntityDetail) -> bool:
        """Put the search of the entity's contents below the sections (once). False when
        the window gave no way to make one: then the section shows a count."""
        if self._make_contents is None:
            return False
        if self.contents is None:
            types = tuple(contained_types(self.session.schema, detail.type))
            spec = SearchSpec(types=types, within=self.entity_id)
            self.contents = self._make_contents(detail.type, spec)
            self.contents.selection_changed.connect(self.selection_changed)
            in_search = QPushButton("Show in search")
            in_search.setFlat(True)
            in_search.setCursor(Qt.CursorShape.PointingHandCursor)
            in_search.setToolTip("Open Search all with a Within chip for these contents")
            in_search.clicked.connect(lambda: self.show_in_search.emit(self.entity_id))
            self.contents.header_row.insertWidget(1, in_search)
            self.splitter.addWidget(self.contents)
            self.splitter.setStretchFactor(0, 2)
            self.splitter.setStretchFactor(1, 3)
        return True

    def _show_breadcrumbs(self, detail: EntityDetail) -> None:
        """The containers above the entity, each a link to its page."""
        crumbs = [f'<a href="{c.id}">{html.escape(c.title)}</a>' for c in detail.breadcrumbs]
        text = f" {CRUMB} ".join(crumbs)
        if crumbs:
            text += f" {CRUMB}"
        if detail.other_parents:
            places = "1 other" if detail.other_parents == 1 else f"{detail.other_parents} others"
            text += f" <span style='color:gray'>(also in {places})</span>"
        self.breadcrumbs.setText(text)
        self.breadcrumbs.setVisible(bool(crumbs))

    # --- the header's thumbnail ---

    def _show_thumbnail(self) -> None:
        if self.thumbnails is None:
            return
        loaded = self.thumbnails.get(self.entity_id)
        if loaded is None:
            return  # requested: _thumbnail_ready shows it
        if loaded.image is not None:
            self.thumbnail.setPixmap(_fitted(loaded.image, HEADER_THUMBNAIL))
        else:
            self.thumbnail.setPixmap(icon_for(loaded.icon).pixmap(HEADER_THUMBNAIL // 2))

    def _thumbnail_ready(self, entity_id: int) -> None:
        if entity_id == self.entity_id and shiboken6.isValid(self):
            self._show_thumbnail()

    # --- sections ---

    def _section_widget(self, section: DetailSection) -> QWidget | None:
        body: QWidget | None
        title = section.title
        match section.kind:
            case "fields":
                if not section.fields:
                    return None  # a type without fields: no empty section
                body = self._fields(section)
            case "role":
                body = self._files(section)
            case "gallery":
                body = self._gallery(section)
            case "related":
                body = self._links(section)
                title = f"{title} ({len(section.entities)})"
            case "contents":
                items = "1 item" if section.count == 1 else f"{section.count:,} items"
                body = QLabel(items)
                body.setContentsMargins(12, 0, 0, 0)
            case _:
                return None
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        heading = QLabel(title)
        font = QFont(heading.font())
        font.setBold(True)
        heading.setFont(font)
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFrameShadow(QFrame.Shadow.Sunken)
        row = QHBoxLayout()
        row.addWidget(heading)
        row.addWidget(rule, 1)
        layout.addLayout(row)
        layout.addWidget(body)
        box.setObjectName(f"section_{section.kind}")
        return box

    def _fields(self, section: DetailSection) -> QWidget:
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(12, 0, 0, 0)
        for row in section.fields:
            value = EditableValue(
                row.value, row.type, editable=row.editable, edited=row.edited, label=row.label
            )
            value.setObjectName(f"field_{row.name}")
            value.committed.connect(
                lambda new, name=row.name: self.field_edited.emit(self.entity_id, name, new)
            )
            form.addRow(f"{row.label}:", value)
        return body

    def _files(self, section: DetailSection) -> QWidget:
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(12, 0, 0, 0)
        column.setSpacing(2)
        for file in section.files:
            column.addWidget(_file_label(file))
        if not section.files:
            column.addWidget(QLabel("None"))
        return body

    def _gallery(self, section: DetailSection) -> QWidget:
        gallery = QListWidget()
        gallery.setViewMode(QListView.ViewMode.IconMode)
        gallery.setIconSize(QSize(GALLERY_THUMBNAIL, GALLERY_THUMBNAIL))
        gallery.setResizeMode(QListView.ResizeMode.Adjust)
        gallery.setMovement(QListView.Movement.Static)
        gallery.setWrapping(True)
        gallery.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        gallery.setMinimumHeight(GALLERY_THUMBNAIL + 48)
        items: dict[int, QListWidgetItem] = {}
        for file in section.files:
            item = QListWidgetItem(icon_for("image"), file.relpath.rpartition("/")[2])
            item.setToolTip(file.relpath)
            gallery.addItem(item)
            items[file.resource_id] = item
        if section.files:
            self._load_gallery(gallery, items)
        return gallery

    def _load_gallery(self, gallery: QListWidget, items: dict[int, QListWidgetItem]) -> None:
        resolver = self.session.thumbnails
        ids = list(items)

        def job() -> list[tuple[int, QImage]]:
            found = []
            for resource_id in ids:
                thumb = resolver.resource_thumbnail(resource_id)
                if thumb is not None:
                    image = QImage.fromData(thumb.data)
                    if not image.isNull():
                        found.append((resource_id, image))
            return found

        def done(images: list[tuple[int, QImage]]) -> None:
            if not shiboken6.isValid(gallery):
                return
            for resource_id, image in images:
                items[resource_id].setIcon(QIcon(_fitted(image, GALLERY_THUMBNAIL)))

        run_in_pool(job, on_done=done, pool=self._pool)

    def _links(self, section: DetailSection) -> QWidget:
        links = " · ".join(f'<a href="{e.id}">{html.escape(e.title)}</a>' for e in section.entities)
        label = QLabel(links or "None")
        label.setWordWrap(True)
        label.setContentsMargins(12, 0, 0, 0)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.linkActivated.connect(lambda href: self.open_entity.emit(int(href)))
        return label

    # --- like a search page ---

    def selected_entity_ids(self, on_done: Callable[[list[int]], None]) -> None:
        """What tagging applies to: the items selected in the contents, else the page's
        own entity."""
        own = [self.entity_id] if self.detail is not None else []
        if self.contents is None:
            on_done(own)
            return
        self.contents.selected_entity_ids(lambda ids: on_done(ids or own))


def _file_label(file: FileRow) -> QLabel:
    text = html.escape(file.relpath or "(the root folder)")
    facts = [html.escape(file.root_name)]
    if file.size is not None and file.kind == "file":
        facts.append(f"{file.size:,} bytes")
    if file.status is ResourceStatus.OFFLINE:
        facts.append("<b>offline</b>")
    elif file.status is ResourceStatus.MISSING:
        facts.append('<b style="color:#c0392b">missing</b>')
    label = QLabel(f"{text} <span style='color:gray'>· {' · '.join(facts)}</span>")
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setWordWrap(True)  # long paths wrap rather than widen the page
    label.setToolTip(file.path or "This root has no path on this computer")
    return label


def _fitted(image: QImage, side: int) -> QPixmap:
    """``image`` scaled down (never up) to fit a ``side`` square."""
    pixmap = QPixmap.fromImage(image)
    if pixmap.width() > side or pixmap.height() > side:
        pixmap = pixmap.scaled(
            side,
            side,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return pixmap


def _root_path(session: KeepSession) -> Callable[[str], str | None]:
    def path(root_id: str) -> str | None:
        try:
            return session.root_path(root_id)
        except StopIteration:  # a root no longer in keep.toml
            return None

    return path
