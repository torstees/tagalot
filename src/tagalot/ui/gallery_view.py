"""A gallery on an item's page (DESIGN.md §9 *Galleries*, §12; #386).

A role's pictures in groups (``core.gallery.gallery_groups``): loose pictures, then one
group per subfolder, then one per archive, each headed by its name. An archive's pictures
are listed and drawn in workers; one opens from a copy extracted to the session's
temporary folder. A rar archive without a tool to read it, or one that can't be read, shows
as the archive itself (with why, for a damaged one).
"""

from collections.abc import Callable
from pathlib import Path

import shiboken6
from PySide6.QtCore import QPoint, QSize, Qt, QThreadPool, QTimer
from PySide6.QtGui import QFont, QIcon, QImage, QPainter, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.detail import PATH_MARK, DetailSection, FileRow
from tagalot.core.gallery import (
    ArchiveListing,
    GalleryGroup,
    extract_member,
    gallery_groups,
    list_archive,
)
from tagalot.core.handlers import OPEN, REVEAL, FileToOpen
from tagalot.core.models import ResourceStatus
from tagalot.core.session import KeepSession
from tagalot.core.thumbnails.image import Thumbnail
from tagalot.ui.file_actions import FileOpener
from tagalot.ui.thumbnails import icon_for
from tagalot.ui.workers import run_in_pool

GALLERY_THUMBNAIL = 128
MEMBER = Qt.ItemDataRole.UserRole + 1
"""An archive picture's path inside its archive (a resource's items hold its id instead)."""
READING = "Reading the archive…"


def fitted(image: QImage, size: int) -> QPixmap:
    """``image`` scaled down (never up) to fit a ``size`` box."""
    pixmap = QPixmap.fromImage(image)
    if pixmap.width() > size or pixmap.height() > size:
        pixmap = pixmap.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return pixmap


def tile(image: QImage, size: int = GALLERY_THUMBNAIL) -> QIcon:
    """A gallery tile: ``image`` fitted and centered on a transparent ``size`` square, so
    every tile's name sits at the same height."""
    pixmap = fitted(image, size)
    square = QPixmap(size, size)
    square.fill(Qt.GlobalColor.transparent)
    painter = QPainter(square)
    painter.drawPixmap((size - pixmap.width()) // 2, (size - pixmap.height()) // 2, pixmap)
    painter.end()
    return QIcon(square)


def _image(thumb: Thumbnail | None) -> QImage | None:
    if thumb is None:
        return None
    image = QImage.fromData(thumb.data)
    return None if image.isNull() else image


class GalleryList(QListWidget):
    """Thumbnails that wrap, as tall as they need: the page scrolls, not each group."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.setObjectName(name)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setIconSize(QSize(GALLERY_THUMBNAIL, GALLERY_THUMBNAIL))
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        line = self.fontMetrics().lineSpacing()
        self.cell = QSize(GALLERY_THUMBNAIL + 16, GALLERY_THUMBNAIL + line + 12)
        self.setGridSize(self.cell)  # every tile alike, so rows line up
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.cell.height() + 2 * self.frameWidth() + 4)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self.fit)

    def fit(self) -> None:
        """Be as tall as the wrapped thumbnails (at least one row)."""
        if not shiboken6.isValid(self):
            return
        self.doItemsLayout()
        rows = max(self.contentsSize().height(), self.cell.height())
        height = rows + 2 * self.frameWidth() + 4
        if height != self.height():
            self.setFixedHeight(height)


class GalleryView(QWidget):
    """One gallery section of a page."""

    def __init__(
        self,
        section: DetailSection,
        session: KeepSession,
        entity_id: int,
        file_opener: FileOpener | None,
        file_menu: Callable[[FileRow], QMenu],
        pool: QThreadPool | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName(f"gallery_view_{section.title}")
        self.section = section
        self.session = session
        self.entity_id = entity_id
        self.file_opener = file_opener
        self._file_menu = file_menu
        self._pool = pool
        self.groups = gallery_groups(section.files)
        self.lists: dict[str, GalleryList] = {}
        """Each group's list, by its title (``""`` for loose pictures)."""
        self.listings: dict[str, ArchiveListing] = {}
        """What each archive group found, once listed."""
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(0, 0, 0, 0)
        if not self.groups:  # an empty gallery: one empty list, as before
            self._resource_list("", [])
        for group in self.groups:
            if group.kind == "archive":
                self._archive_group(group)
            else:
                self._resource_list(group.title, list(group.files))

    # --- groups ---

    def _header(self, title: str) -> None:
        header = QLabel(title)
        font = QFont(header.font())
        font.setBold(True)
        header.setFont(font)
        header.setTextFormat(Qt.TextFormat.PlainText)
        header.setObjectName("gallery_header")
        self.column.addWidget(header)

    def _list(self, title: str) -> GalleryList:
        name = f"gallery_{self.section.title}" + (f":{title}" if title else "")
        gallery = GalleryList(name)
        self.lists[title] = gallery
        self.column.addWidget(gallery)
        return gallery

    def _resource_list(self, title: str, files: list[FileRow]) -> GalleryList:
        """Pictures that are files: open and menu as file rows (#124)."""
        if title:
            self._header(title)
        gallery = self._list(title)
        rows = {f.resource_id: f for f in files}
        items: dict[int, QListWidgetItem] = {}
        for file in files:
            item = QListWidgetItem(icon_for("image"), file.relpath.rpartition("/")[2])
            item.setToolTip(file.relpath)
            item.setData(Qt.ItemDataRole.UserRole, file.resource_id)
            gallery.addItem(item)
            items[file.resource_id] = item
        gallery.itemActivated.connect(
            lambda item: self._open_file(item.data(Qt.ItemDataRole.UserRole))
        )
        gallery.customContextMenuRequested.connect(
            lambda point: self._resource_menu(gallery, rows, point)
        )
        if items:
            self._load_resources(gallery, items)
        gallery.fit()
        return gallery

    def _archive_group(self, group: GalleryGroup) -> None:
        archive = group.files[0]
        if archive.status is not ResourceStatus.OK or not archive.path:
            self._resource_list(group.title, [archive])  # offline or missing: the file
            return
        self._header(group.title)
        note = QLabel(READING)
        note.setObjectName("gallery_note")
        note.setWordWrap(True)
        self.column.addWidget(note)
        gallery = self._list(group.title)
        gallery.hide()
        path = archive.path
        run_in_pool(
            lambda: list_archive(path),
            on_done=lambda listing: self._listed(group, archive, note, gallery, listing),
            pool=self._pool,
        )

    def _listed(
        self,
        group: GalleryGroup,
        archive: FileRow,
        note: QLabel,
        gallery: GalleryList,
        listing: ArchiveListing,
    ) -> None:
        if not shiboken6.isValid(self):
            return
        self.listings[group.title] = listing
        if listing.no_tool or listing.error is not None or not listing.pictures:
            # Shown as the archive itself, as before galleries looked inside (#386).
            if listing.error is not None:
                note.setText(f"Couldn't read this archive: {listing.error}")
            elif listing.no_tool:
                note.setText("Install unrar, 7-Zip, or bsdtar to see the pictures inside.")
            else:
                note.setText("No pictures inside.")
            gallery.deleteLater()
            index = self.column.indexOf(note) + 1
            replacement = self._resource_list("", [archive])
            replacement.setObjectName(f"gallery_{self.section.title}:{group.title}")
            self.lists[group.title] = replacement
            self.column.insertWidget(index, replacement)
            return
        note.setText(f"and {listing.more:,} more" if listing.more else "")
        note.setVisible(bool(listing.more))
        for picture in listing.pictures:
            icon = icon_for("archive" if picture.nested else "image")
            item = QListWidgetItem(icon, picture.name.rpartition("/")[2])
            item.setToolTip(f"{group.title} {PATH_MARK} {picture.name}")
            item.setData(MEMBER, picture.name)
            gallery.addItem(item)
        gallery.show()
        gallery.fit()
        gallery.itemActivated.connect(lambda item: self.open_member(archive, item.data(MEMBER)))
        gallery.customContextMenuRequested.connect(
            lambda point: self._member_menu(gallery, archive, point)
        )
        names = [p.name for p in listing.pictures if p.readable and not p.nested]
        if names:
            self._load_members(gallery, archive, names)

    # --- thumbnails ---

    def _load_resources(self, gallery: GalleryList, items: dict[int, QListWidgetItem]) -> None:
        resolver, ids = self.session.thumbnails, list(items)

        def job() -> list[tuple[int, QImage]]:
            found = []
            for resource_id in ids:
                image = _image(resolver.resource_thumbnail(resource_id))
                if image is not None:
                    found.append((resource_id, image))
            return found

        def done(images: list[tuple[int, QImage]]) -> None:
            if shiboken6.isValid(gallery):
                for resource_id, image in images:
                    items[resource_id].setIcon(tile(image))

        run_in_pool(job, on_done=done, pool=self._pool)

    def _load_members(self, gallery: GalleryList, archive: FileRow, names: list[str]) -> None:
        resolver, resource_id = self.session.thumbnails, archive.resource_id

        def job() -> list[tuple[str, QImage]]:
            pairs = resolver.archive_thumbnails(resource_id, names)
            return [(name, image) for name, thumb in pairs if (image := _image(thumb))]

        def done(images: list[tuple[str, QImage]]) -> None:
            if not shiboken6.isValid(gallery):
                return
            items = {gallery.item(i).data(MEMBER): gallery.item(i) for i in range(gallery.count())}
            for name, image in images:
                if name in items:
                    items[name].setIcon(tile(image))

        run_in_pool(job, on_done=done, pool=self._pool)

    # --- opening ---

    def _open_file(self, resource_id: object) -> None:
        if self.file_opener is not None and isinstance(resource_id, int):
            opener = self.file_opener
            opener.lookup(self.entity_id, resource_id, lambda f: opener.act(f, OPEN))

    def open_member(self, archive: FileRow, name: object) -> None:
        """Extract a picture from the archive to the temporary folder, in a worker, and
        open the copy."""
        if self.file_opener is None or not isinstance(name, str) or not archive.path:
            return
        opener, path = self.file_opener, archive.path
        folder = self.session.temp_dir() / "archives" / str(archive.resource_id)

        def done(copy: Path) -> None:
            opener.act(FileToOpen(archive.resource_id, str(copy), False), OPEN)

        def failed(error: BaseException) -> None:
            opener.message.emit(f"Couldn't open {name} from {archive.relpath}: {error}")

        run_in_pool(
            lambda: extract_member(path, name, folder),
            on_done=done,
            on_error=failed,
            pool=self._pool,
        )

    def _resource_menu(self, gallery: GalleryList, rows: dict[int, FileRow], point: QPoint) -> None:
        item = gallery.itemAt(point)
        file = rows.get(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None
        if file is not None:
            self._file_menu(file).exec(gallery.viewport().mapToGlobal(point))

    def member_menu(self, archive: FileRow, name: str) -> QMenu:
        """The right-click menu of a picture inside an archive."""
        menu = QMenu(self)
        open_action = menu.addAction("Open")
        open_action.setToolTip("Open a copy with its usual program; the archive isn't changed")
        open_action.triggered.connect(lambda: self.open_member(archive, name))
        reveal = menu.addAction("Show the archive in file manager")
        reveal.triggered.connect(lambda: self._reveal(archive))
        return menu

    def _member_menu(self, gallery: GalleryList, archive: FileRow, point: QPoint) -> None:
        item = gallery.itemAt(point)
        if item is not None:
            menu = self.member_menu(archive, item.data(MEMBER))
            menu.exec(gallery.viewport().mapToGlobal(point))

    def _reveal(self, archive: FileRow) -> None:
        if self.file_opener is not None:
            opener = self.file_opener
            opener.lookup(self.entity_id, archive.resource_id, lambda f: opener.act(f, REVEAL))
