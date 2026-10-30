"""An entity's detail page (DESIGN.md §12 "Detail page").

```
┌────────┐  Blue Train                         │ Tags │ Details │
│ thumb  │  Album                              │ Details ──────  │
└────────┘                                     │  Year           │
Songs (5) │ Scans (3)                          │    1957         │
  [filter bar]                                 │ Folder ───────  │
  [results: list or grid]                      │  Jazz/Coltrane… │
```

A header with the thumbnail and title, and below it the searches: a container's contents
(``within`` the entity), with a filter bar and the usual layouts, and related sections;
several are tabs: the contents split by type (an artist's Albums and Songs, each counted,
empty ones hidden, #456), related sections, then galleries; the tab last used on a type of
page is chosen again. The theme's other sections (Details, Extra fields, files, …) are in
:attr:`DetailPage.details`, which the window shows in the Details tab beside Tags (#468);
galleries that aren't tabs stay below the header.
Everything is read in a worker (:func:`~tagalot.core.detail.load_detail`); related entities
are links that open their own pages.
"""

import html
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QFont, QIcon, QImage, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.actions import actions_for
from tagalot.core.archive_choices import is_archive
from tagalot.core.assets import assets_folders
from tagalot.core.containers import hand_made_types
from tagalot.core.detail import DetailSection, EntityDetail, FileRow, load_detail
from tagalot.core.handlers import OPEN
from tagalot.core.ingest import TITLE
from tagalot.core.item_pictures import USER, is_picture
from tagalot.core.keep import KeepConfig
from tagalot.core.keywords import ItemKeyword, KeywordInfo
from tagalot.core.markdown_glimpse import glimpse
from tagalot.core.models import FieldSource, ResourceStatus
from tagalot.core.note_markdown import find_links, read_pictures, renderable
from tagalot.core.notes import read_note
from tagalot.core.own_fields import OWN
from tagalot.core.roots import local_path
from tagalot.core.search import POSITION
from tagalot.core.search_fields import contained_types, contents_order, type_contents_order
from tagalot.core.search_spec import SearchSpec, SortKey
from tagalot.core.session import KeepSession
from tagalot.core.tags import entity_tags
from tagalot.themes.api import entity_plural
from tagalot.ui.archive_actions import ArchiveActions
from tagalot.ui.colors import ERROR, css, dimmed_css
from tagalot.ui.field_editor import EditableValue
from tagalot.ui.field_filters import CLOSE_MARK
from tagalot.ui.file_actions import FileOpener, add_file_actions, file_kind
from tagalot.ui.fonts import follow, place_font
from tagalot.ui.gallery_view import GalleryView, fitted
from tagalot.ui.icon_pictures import icon_html
from tagalot.ui.icons import icon, icon_for, lucide
from tagalot.ui.kind_chips import KindChips, kind_label, kind_tip
from tagalot.ui.lookups import LOOK_UP_TIP
from tagalot.ui.models.results import BadgesValue, badges_value
from tagalot.ui.note_view import NoteView
from tagalot.ui.search_view import WRITE_BACK_TIP, SearchPage, add_reread_actions
from tagalot.ui.thumbnails import ThumbnailLoader
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

HEADER_MAX_SHARE = 0.6
"""The most of a page's height the header takes above its tabs at first (#491)."""
HEADER_THUMBNAIL = 192
"""The header thumbnail's box, in pixels."""


NOTE_PICTURE_WIDTH = 480
"""A note's pictures are shown at most this wide, in pixels."""

CRUMB = "\u203a"
"""Between breadcrumbs."""

FROM_NOTE_TIP = (
    "From this item's note: scans keep it up to date. Edit it to make it yours; "
    "remove it and scans leave it off (Undo puts it back)"
)
FROM_FILE_TIP = (
    "From this item's file (its front matter): scans keep it up to date. Edit it to make "
    "it yours; remove it and scans leave it off (Undo puts it back)"
)
IGNORE_KEY = "Ignore this key in every note…"
ALIAS_KEY = "Read this key as a field…"
MAKE_FIELD = "Make it a field…"
TURN_BACK = "Turn back into an Extra field"


@dataclass
class _Loaded:
    """What a page's worker read: the item, and its note's folder and pictures."""

    detail: EntityDetail | None
    note_folder: str | None = None
    pictures: dict[str, QImage] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)
    """Where the note's links are, when in an assets folder rather than beside it."""
    glimpse: str = ""
    """A glimpse of the item's own Markdown file (#554), shown when it has no note."""
    badges: BadgesValue | None = None
    """The item's tag badges (#398), shown beside its title."""


def glimpsed_file(detail: EntityDetail) -> FileRow | None:
    """The item's own Markdown file, when the page shows a glimpse of it rather than a
    note (#554): the note, typed or a file, wins."""
    if detail.note_body.strip() or detail.note_file is not None:
        return None
    return detail.markdown_file


def note_assets(
    detail: EntityDetail, config: KeepConfig, root_path: Callable[[str], str | None]
) -> list[str]:
    """The assets folders (this computer's paths) the item's note's pictures and links are
    looked for in when not beside it (#418): the item's own, its folder's, the keep's. For
    a glimpse of its own Markdown file (#554), that file's."""
    file = glimpsed_file(detail) or detail.note_file
    base = root_path(file.root_id) if file is not None else None
    if file is None or base is None:
        return []
    root = next((r for r in config.roots if r.id == file.root_id), None)
    defaults = (root.assets if root is not None else "", config.markdown_assets)
    return [local_path(base, f) for f in assets_folders(file.relpath, detail.assets, defaults)]


def _read_note(detail: EntityDetail | None, assets: Sequence[str] = ()) -> _Loaded:
    """The note's folder on this computer and its pictures, read here in the worker, and
    where its links are when they're in one of the ``assets`` folders. For an item without
    a note whose own file is Markdown, a glimpse of that file (#554), read here too; while
    its folder is offline, nothing."""
    if detail is None:
        return _Loaded(detail)
    glimpsed = glimpsed_file(detail)
    file = glimpsed or detail.note_file
    if file is None or file.path is None:
        return _Loaded(detail)
    folder = os.path.dirname(file.path)
    pictures = {}
    links: dict[str, str] = {}
    text = ""
    if file.status is ResourceStatus.OK:
        source = detail.note_body
        if glimpsed is not None:
            try:
                text = glimpse(read_note(file.path).body, detail.title)
            except (OSError, ValueError) as e:
                logger.info("Not showing %s on its page: %s", file.path, e)
            source = text
        body = renderable(source)
        links = find_links(body, folder, assets)
        for name, data in read_pictures(body, folder, assets).items():
            image = QImage.fromData(data)
            if image.width() > NOTE_PICTURE_WIDTH:
                image = image.scaledToWidth(
                    NOTE_PICTURE_WIDTH, Qt.TransformationMode.SmoothTransformation
                )
            if not image.isNull():
                pictures[name] = image
    return _Loaded(detail, folder, pictures, links, glimpse=text)


def related_key(session: KeepSession, name: str, side: str) -> str:
    """Which of a page's related searches a section is: by relationship, and for a
    relationship of a type with itself, by side too (Cites, and Cited by as ``cites:b``)."""
    link = session.schema.relationships.get(name)
    if link is not None and link.relationship.a is link.relationship.b and side == "b":
        return f"{name}:b"
    return name


class DetailPage(QWidget):
    """The detail page of one entity. Emits :attr:`open_entity` with an entity id when a
    related entity's link is clicked."""

    open_entity = Signal(int)
    field_edited = Signal(int, str, object)
    """The user set a field (or ``"title"``) by hand: entity id, field name, new value."""
    extra_edited = Signal(int, str, object, bool)
    """An extra field was added (last argument true), changed, or removed (value
    ``None``): entity id, name, value, whether it is new."""
    note_key_ignore_requested = Signal(str)
    """Ignore this front-matter key in every note of the keep (#381); the user agreed."""
    note_key_alias_requested = Signal(str, str, str)
    """Read this front-matter key as a field (#411): the key, the field's name, and its
    label."""
    promote_requested = Signal(str, str)
    """**Make it a field…** on an Extra field (#582): its key and the item's type."""
    turn_back_requested = Signal(str, str)
    """**Turn back into an Extra field** on one of the keep's own fields (#582): the
    item's type and the field's ``own:<name>``."""
    note_edited = Signal(int, str)
    """The user typed the item's note (#383): entity id, the Markdown (empty: removed)."""
    save_note_requested = Signal(int)
    """Save as note… (#383): write the item's typed note as a file in the notes folder."""
    reread_requested = Signal(list, bool)
    write_back_requested = Signal(list)
    look_up_requested = Signal(list)
    """Look up online (#340): the item's id, in a list."""
    """Write to file… on this item (#299)."""
    unlink_requested = Signal(int, int, str)
    place_unlink_requested = Signal(int)
    """**Unlink** on an Also at row (#600): the place's resource id."""
    """Remove a link made by hand: (entity id, resource id, role)."""
    picture_requested = Signal(int, object)
    """Make a file the item's own picture (#439): entity id, resource id, or ``None`` to
    use the usual picture again."""
    choose_picture_requested = Signal(int)
    """Set picture…: ask for a picture file for this item."""
    relate_requested = Signal(int, str, str)
    """Add to a related section by hand (#260): (entity id, relationship name)."""
    unrelate_requested = Signal(int, str, list, str)
    uncontain_requested = Signal(int, list)
    """Remove from project (#329): (this container's id, the items to take out)."""
    move_requested = Signal(int, str, list, int)
    """Reorder an ordered related list (#317): (this item's id, relationship, the items
    moved, -1 up or 1 down)."""
    """Remove related items by hand: (entity id, relationship name, their ids)."""
    action_requested = Signal(str, list)
    """Run a theme action on the page's item: (method name, [entity id])."""
    """Read the entity's files again; true: replacing what the user edited."""
    keyword_map_requested = Signal(object)
    """A :class:`~tagalot.core.keywords.KeywordInfo` to map to a tag (#295)."""
    keyword_ignore_requested = Signal(list, bool)
    """Keyword keys, and whether to ignore them."""
    file_tag_restore_requested = Signal(int, int)
    """Entity id and tag id: put back a file tag the user removed."""
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
        make_contents: Callable[[str, SearchSpec, str], SearchPage] | None = None,
        pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.entity_id = entity_id
        """The item shown: the one asked for, or the item it was merged into (§13)."""
        self.requested_id = entity_id
        """The item the page was opened for. Each refresh asks for it again, so undoing a
        merge brings its page back to it."""
        self.thumbnails = thumbnails
        self._make_contents = make_contents
        self.contents_pages: dict[str, SearchPage] = {}
        """The searches of a container's contents, once the page knows it is one: one for
        all of it (``""``), or one per contained type (#456), by type id."""
        self.related: dict[str, SearchPage] = {}
        """Each related section's search, by relationship name (#125)."""
        self._bottom: QWidget | None = None
        """What sits below the sections: one embedded search, or tabs of several."""
        self._tabs: QTabWidget | None = None
        self._bottom_label = ""
        self._bottom_icon = QIcon()
        self._bottom_key = ""
        self._tab_keys: dict[int, str] = {}
        """Each tab's widget (by ``id``) -> what it is, remembered as the last tab used:
        ``contents:<type>``, ``related:<name>``, ``gallery:<title>``."""
        self._gallery_tabs: list[QWidget] = []
        """The galleries shown as tabs (made again on each refresh)."""
        self._counts: dict[str, int] = {}
        """Each contents tab's count, by type id, once counted."""
        self.recall_tab: Callable[[str], str | None] = lambda type_id: None
        """The tab last used on a page of this type (the window remembers it)."""
        self.remember_tab: Callable[[str, str], None] = lambda type_id, key: None
        self.recall_kinds: Callable[[str], list[str]] = lambda key: []
        """The kinds hidden on a gallery tab, by its state key (the window remembers it)."""
        self.remember_kinds: Callable[[str, list[str]], None] = lambda key, kinds: None
        self.title_in_details = False
        """Name the item at the top of :attr:`details`: for a search's selected item, whose
        page isn't shown (#468)."""
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
        # In the keep's titles font (#565), following its changes.
        follow(self.title, lambda: place_font("titles", bold=True, larger=5))
        follow(self.title_value.box, lambda: place_font("titles", bold=True, larger=5))
        self.type_label = QLabel()
        self.type_label.setStyleSheet("color: palette(placeholder-text);")
        self.type_icon = QLabel()  # the type's own (#556)
        self.type_icon.setObjectName("type_icon")
        self.breadcrumbs = QLabel()
        self.breadcrumbs.setTextFormat(Qt.TextFormat.RichText)
        self.breadcrumbs.setWordWrap(True)
        self.breadcrumbs.linkActivated.connect(lambda href: self.open_entity.emit(int(href)))
        self.breadcrumbs.setVisible(False)
        self.merged_note = QLabel("The item opened here was merged into this one.")
        self.merged_note.setObjectName("merged_note")
        self.merged_note.setStyleSheet("color: palette(placeholder-text); font-style: italic;")
        self.merged_note.setVisible(False)
        # The badges of the item's own tags, beside its title (#398): all of them, since the
        # page has room; their tooltip names the tags.
        self.badges = QLabel()
        self.badges.setObjectName("badges")
        follow(self.badges, lambda: place_font("text", larger=5))
        self.badges.setVisible(False)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(self.title_value, 1)
        title_row.addWidget(self.badges, 0, Qt.AlignmentFlag.AlignVCenter)
        titles = QVBoxLayout()
        titles.addWidget(self.merged_note)
        titles.addWidget(self.breadcrumbs)
        titles.addLayout(title_row)
        type_row = QHBoxLayout()
        type_row.setContentsMargins(0, 0, 0, 0)
        type_row.addWidget(self.type_icon)
        type_row.addWidget(self.type_label, 1)
        titles.addLayout(type_row)
        self.note_view = NoteView()
        self.note_view.open_note.connect(self.open_note)
        self.note_view.edited.connect(lambda text: self.note_edited.emit(self.entity_id, text))
        self.note_view.resized.connect(lambda: QTimer.singleShot(0, self._fit_header))
        titles.addWidget(self.note_view)
        titles.addStretch(1)
        self._titles = titles
        self.more_button = QToolButton()
        self.more_button.setText("More \u25be")
        self.more_button.setAutoRaise(True)
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more = QMenu(self.more_button)
        self._more_menu = more
        self._file_actions_added = False
        self._write_back_added = False
        self._look_up_added = False
        self.file_opener: FileOpener | None = None
        self.archive_actions: ArchiveActions | None = None
        """What to read inside archives (#519): set by the window."""
        """Opens the item's files (the window sets it); without one, no file actions."""
        self.open_note_action = more.addAction("Open note")
        self.open_note_action.setObjectName("open_note_action")
        self.open_note_action.setToolTip("Edit this item's note in its own program")
        self.open_note_action.triggered.connect(self.open_note)
        self.open_note_action.setVisible(False)
        self.save_note_action = more.addAction("Save as note\u2026")
        self.save_note_action.setObjectName("save_note_action")
        self.save_note_action.setToolTip(
            "Write this item's note as a new file in the notes folder; from then on it's "
            "edited there"
        )
        self.save_note_action.triggered.connect(
            lambda: self.save_note_requested.emit(self.entity_id)
        )
        self.save_note_action.setVisible(False)
        self.set_picture_action = more.addAction("Set picture\u2026")
        self.set_picture_action.setObjectName("set_picture_action")
        self.set_picture_action.setToolTip(
            "Choose a picture file for this item, shown wherever it is (Edit \u2192 Undo)"
        )
        self.set_picture_action.triggered.connect(
            lambda: self.choose_picture_requested.emit(self.entity_id)
        )
        self.usual_picture_action = more.addAction("Use the usual picture")
        self.usual_picture_action.setObjectName("usual_picture_action")
        self.usual_picture_action.setToolTip(
            "Forget the picture you chose: its note's, or the theme's, is shown again"
        )
        self.usual_picture_action.triggered.connect(
            lambda: self.picture_requested.emit(self.entity_id, None)
        )
        self.usual_picture_action.setVisible(False)
        more.addSeparator()
        add_reread_actions(
            more, lambda replace: self.reread_requested.emit([self.entity_id], replace)
        )
        self.more_button.setMenu(more)
        header = QHBoxLayout()
        header.addWidget(self.thumbnail, 0, Qt.AlignmentFlag.AlignTop)
        header.addSpacing(12)
        header.addLayout(titles, 1)
        self._action_buttons = QHBoxLayout()
        self._action_buttons.setSpacing(4)
        header.addLayout(self._action_buttons)
        header.addWidget(self.more_button, 0, Qt.AlignmentFlag.AlignTop)

        # The theme's sections go in the Details tab beside Tags (#468), the window's to
        # place; until it does, the panel is a hidden child of the page.
        self._sections = QVBoxLayout()
        self._sections.setSpacing(14)
        self.details_title = QLabel()
        self.details_title.setObjectName("details_title")
        self.details_title.setWordWrap(True)
        self.details_title.setTextFormat(Qt.TextFormat.RichText)
        self.details_title.linkActivated.connect(lambda _: self.open_entity.emit(self.entity_id))
        self.details_title.setVisible(False)
        panel = QWidget()
        panel_column = QVBoxLayout(panel)
        panel_column.addWidget(self.details_title)
        panel_column.addLayout(self._sections)
        panel_column.addStretch(1)
        self.details = QScrollArea(self)
        """The item's sections, shown in the window's Details tab."""
        self.details.setObjectName("details_panel")
        self.details.setWidgetResizable(True)
        self.details.setFrameShape(QFrame.Shape.NoFrame)
        # As wide as the tab: long values wrap, rather than scroll sideways.
        self.details.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.details.setWidget(panel)
        self.details.hide()
        # Galleries that aren't tabs stay below the header, in the middle.
        self._galleries = QVBoxLayout()
        self._galleries.setSpacing(14)
        content = QWidget()
        column = QVBoxLayout(content)
        column.addLayout(header)
        column.addSpacing(8)
        column.addLayout(self._galleries)
        column.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        self._middle = content
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(scroll)
        self._divider_dragged = False
        """The user moved the divider above the tabs: the header no longer fits itself."""
        self.splitter.splitterMoved.connect(self._divider_moved)
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
        generation, session, entity_id = self._generation, self.session, self.requested_id
        config, roots = session.keep.config, _root_path(session)
        own_fields = session.own_fields

        def job() -> _Loaded:
            tree = session.tag_cache.get()
            with session.reader.connect() as conn:
                detail = load_detail(conn, session.schema, entity_id, roots, own_fields)
                own = entity_tags(conn, [detail.id]).get(detail.id, []) if detail else []
            assets = note_assets(detail, config, roots) if detail is not None else []
            loaded = _read_note(detail, assets)
            loaded.badges = badges_value(tree, own)
            return loaded

        def done(loaded: _Loaded) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._show(loaded.detail)
                self._show_note(loaded)
                self._show_badges(loaded.badges)
                QTimer.singleShot(0, self._fit_header)  # once the header has its new size

        run_in_pool(job, on_done=done, pool=self._pool)

    def _show_badges(self, badges: BadgesValue | None) -> None:
        icons = badges.icons if badges is not None else ()
        self.badges.setVisible(bool(icons))
        if all(isinstance(i, str) for i in icons):
            self.badges.setTextFormat(Qt.TextFormat.PlainText)
            self.badges.setText(" ".join(str(i) for i in icons))
        else:  # with pictures (#507): rich text, each a line of text high
            side = self.badges.fontMetrics().height()
            self.badges.setTextFormat(Qt.TextFormat.RichText)
            self.badges.setText(
                "&nbsp;".join(
                    html.escape(i) if isinstance(i, str) else icon_html(i, side) for i in icons
                )
            )
        self.badges.setToolTip(badges.tooltip if badges is not None else "")

    def _show(self, detail: EntityDetail | None) -> None:
        # While the item asked for is merged into another (§13), the page is that one's;
        # after an undo it is the item's own again.
        merged = detail is not None and detail.merged_from is not None
        self.entity_id = detail.id if detail is not None else self.requested_id
        self.merged_note.setVisible(merged)
        self.detail = detail
        for layout in (self._sections, self._galleries):
            while layout.count():
                item = layout.takeAt(0)
                if item is not None and (widget := item.widget()) is not None:
                    widget.deleteLater()
        self._show_details_title(detail)
        if detail is None:
            self.title_value.editable = False
            self.title_value.show_value("This item no longer exists", False)
            self.type_label.setText("")
            self.type_icon.clear()
            self.thumbnail.clear()
            self.note_view.setVisible(False)
            self.open_note_action.setVisible(False)
            self.save_note_action.setVisible(False)
        else:
            self.title_value.editable = True
            self.title_value.field_label = detail.title_label.lower()
            self._show_picture_source(detail)
            self.title_value.show_value(detail.title, detail.title_edited)
            self.type_label.setText(detail.type_label)
            side = self.type_label.fontMetrics().height()
            self.type_icon.setPixmap(
                self._type_icon(detail.type).pixmap(side, mode=QIcon.Mode.Disabled)
            )
            self._add_file_actions(detail)
            self._add_write_back(detail)
            self._add_look_up()
            self._show_action_buttons(detail)
            self._show_breadcrumbs(detail)
            extras = self._extra_widget(detail)
            # With searches below the sections, galleries are tabs there too, last (#456).
            tabbed = self._make_contents is not None and any(
                s.kind == "contents" or (s.kind == "related" and s.relationship and s.other_type)
                for s in detail.sections
            )
            galleries: list[DetailSection] = []
            for section in detail.sections:
                if section.kind == "contents" and self._show_contents(detail):
                    continue
                if section.kind == "related" and self._show_related(section):
                    continue
                if section.kind == "gallery" and tabbed:
                    galleries.append(section)
                    continue
                widget = self._section_widget(section)
                if widget is not None:  # a gallery under the header, the rest in Details
                    place = self._galleries if section.kind == "gallery" else self._sections
                    place.addWidget(widget)
                if section.kind == "fields" and extras.parent() is None:
                    self._sections.addWidget(extras)  # the user's fields follow the theme's
            if extras.parent() is None:
                self._sections.insertWidget(0, extras)
            if detail.keywords:
                self._sections.addWidget(
                    self._titled("From the file", self._keywords(detail), "section_keywords")
                )
            self._show_gallery_tabs(galleries)
            self._choose_tab()
            self._show_thumbnail()
        self.loaded.emit()

    def _show_details_title(self, detail: EntityDetail | None) -> None:
        """The item's name and type above its details, when its page isn't shown."""
        self.details_title.setVisible(self.title_in_details and detail is not None)
        if detail is not None:
            self.details_title.setText(
                f'<b><a href="open" style="text-decoration: none">{html.escape(detail.title)}'
                f"</a></b><br><span style='color: {dimmed_css()}'>"
                f"{html.escape(detail.type_label)}</span>"
            )
            self.details_title.setToolTip("Open its page")

    @property
    def contents(self) -> SearchPage | None:
        """The contents search shown: the current tab's, else the first."""
        shown = self._tabs.currentWidget() if self._tabs is not None else self._bottom
        pages = list(self.contents_pages.values())
        if isinstance(shown, SearchPage) and shown in pages:
            return shown
        return pages[0] if pages else None

    def _show_contents(self, detail: EntityDetail) -> bool:
        """Put the search of the entity's contents below the sections (once): one search,
        or, when it can hold several types, a tab of each (#456). False when the window gave
        no way to make one: then the section shows a count."""
        if self._make_contents is None:
            return False
        if not self.contents_pages:
            schema = self.session.schema
            types = tuple(contained_types(schema, detail.type))
            if not types:
                sort, _ = contents_order(schema, detail.type)
                spec = SearchSpec(types=types, within=self.requested_id, sort=sort)
                page = self._make_contents(f"contents:{detail.type}", spec, "Contents")
                self._contents_page(detail, page, "", "Contents")
            for type_id in types:
                sort = type_contents_order(schema, detail.type, type_id)
                spec = SearchSpec(types=(type_id,), within=self.requested_id, sort=sort)
                label = entity_plural(schema.by_type_id(type_id).entity)
                # One type keeps the layout remembered for the container's contents.
                key = f"contents:{detail.type}" + (f":{type_id}" if len(types) > 1 else "")
                self._contents_page(detail, self._make_contents(key, spec, label), type_id, label)
        return True

    def _contents_page(
        self, detail: EntityDetail, page: SearchPage, type_id: str, label: str
    ) -> None:
        """Set up one contents search and put it below the sections."""
        self.contents_pages[type_id] = page
        page.selection_changed.connect(self.selection_changed)
        in_search = QPushButton("Show in search")
        in_search.setFlat(True)
        in_search.setCursor(Qt.CursorShape.PointingHandCursor)
        in_search.setToolTip("Open Search all with a Within chip for these contents")
        in_search.clicked.connect(lambda: self.show_in_search.emit(self.entity_id))
        page.header_row.insertWidget(1, in_search)
        if detail.type in hand_made_types(self.session.schema):  # a project (#329)
            page.add_menu_action(
                f"Remove from {detail.title}",
                lambda ids: self.uncontain_requested.emit(self.entity_id, ids),
            )
        if type_id:  # a tab of one type: counted, and hidden while empty (#456)
            page.counted.connect(lambda total: self._contents_counted(type_id, label, total))
            page.kinds_counted.connect(
                lambda: self._contents_counted(type_id, label, page.shown_total)
            )
        self._embed(page, label, f"contents:{type_id or detail.type}", self._type_icon(type_id))

    def _contents_counted(self, type_id: str, label: str, shown: int | None) -> None:
        """Show a contents tab's count (``9 of 16`` while kind chips hide some, #513), and
        hide it while it has nothing (unless all of them have nothing and there are no
        other tabs: then the first stays, saying so). A tab whose items are all hidden by
        its chips stays, so they can be shown again."""
        if self._tabs is None or not shiboken6.isValid(self._tabs) or shown is None:
            return
        page = self.contents_pages[type_id]
        everything = page.unhidden_total
        total = shown if everything is None or not page.current_spec().hidden_kinds else everything
        self._counts[type_id] = total
        index = self._tabs.indexOf(page)
        if index < 0:
            return
        if total and shown != total:
            text = f"{label} ({shown:,} of {total:,})"
        else:
            text = f"{label} ({total:,})" if total else label
        self._tabs.setTabText(index, text)
        typed = [t for t in self.contents_pages if t]
        empty = {t for t in typed if self._counts.get(t) == 0}
        # All empty: the first says so, unless other tabs fill the place (an actor's).
        others = any(
            not self._tab_keys.get(id(self._tabs.widget(i)), "").startswith("contents:")
            for i in range(self._tabs.count())
        )
        for t in typed:
            alone = len(empty) == len(typed) and t == typed[0] and not others
            shown = t not in empty or alone
            self._tabs.setTabVisible(self._tabs.indexOf(self.contents_pages[t]), shown)
        self._choose_tab()

    def _show_related(self, section: DetailSection) -> bool:
        """A related section (a movie's cast, an actor's filmography) as a search of its own
        below the sections, with list and grid, Add…, and "Remove from …" on its items'
        menus (once). False when the window gave no way to make one: then it's links."""
        name = section.relationship
        if self._make_contents is None or name is None or section.other_type is None:
            return False
        side = section.side or ""
        key = related_key(self.session, name, side)
        if key not in self.related:
            ordered = self._orders(name)
            sort = (SortKey(POSITION),) if ordered else (SortKey("title"),)
            spec = SearchSpec(
                types=(section.other_type,),
                related=(name, self.requested_id),
                related_side=section.side,
                sort=sort,
            )
            page = self._make_contents(f"related:{section.other_type}", spec, section.title)
            page.selection_changed.connect(self.selection_changed)
            add = QPushButton("Add\u2026")
            add.setObjectName(f"add_{name}")
            add.setFlat(True)
            add.setCursor(Qt.CursorShape.PointingHandCursor)
            add.setToolTip(f"Add to {section.title.lower()} by hand; scans won't remove it")
            add.clicked.connect(lambda: self.relate_requested.emit(self.entity_id, name, side))
            page.header_row.insertWidget(1, add)
            page.add_menu_action(
                f"Remove from {section.title.lower()}",
                lambda ids: self.unrelate_requested.emit(self.entity_id, name, ids, side),
            )
            if ordered:  # a paper's authors: their order can change (#317)
                page.add_menu_action(
                    "Move up", lambda ids: self.move_requested.emit(self.entity_id, name, ids, -1)
                )
                page.add_menu_action(
                    "Move down",
                    lambda ids: self.move_requested.emit(self.entity_id, name, ids, 1),
                )
            self.related[key] = page
            self._embed(page, section.title, f"related:{key}", self._type_icon(section.other_type))
        return True

    def _orders(self, name: str) -> bool:
        """Whether this page's item has its own order of relationship ``name``'s items (it
        is the ``b`` side of an ordered relationship: a paper, for its authors)."""
        link = self.session.schema.relationships.get(name)
        if link is None or not link.relationship.ordered or self.detail is None:
            return False
        return self.session.schema.theme.type_id_of(link.relationship.b) == self.detail.type

    # --- the divider above the tabs ---

    def _divider_moved(self, _pos: int, _index: int) -> None:
        self._divider_dragged = True

    def _fit_header(self) -> None:
        """Start the tabs right under the header (and any galleries in the middle), as long
        as that leaves the tabs most of the page; a taller header scrolls (#491). Once the
        user drags the divider, it stays where they put it."""
        if self._bottom is None or self._divider_dragged or not shiboken6.isValid(self):
            return
        total = self.splitter.height() - self.splitter.handleWidth()
        if total <= 0:
            return
        wanted = self._middle.sizeHint().height()
        header = min(wanted, int(total * HEADER_MAX_SHARE))
        self.splitter.setSizes([header, total - header])

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_header()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._fit_header()

    def _type_icon(self, type_id: str | None) -> QIcon:
        """A type's icon (#556), the keep's choice first (#557); the generic one for no
        type, or one not in the theme."""
        return icon(self.session.type_icon(type_id or ""))

    def _embed(self, page: QWidget, label: str, key: str, tab_icon: QIcon) -> None:
        """Put an embedded search (or a gallery) below the sections; with more than one, as
        tabs, each with ``tab_icon``. ``key`` names it for remembering the last tab used."""
        self._tab_keys[id(page)] = key
        if self._bottom is None:
            self._bottom, self._bottom_label, self._bottom_key = page, label, key
            self._bottom_icon = tab_icon
            self.splitter.addWidget(page)
            # The header keeps its height; the searches below get the rest (#468).
            self.splitter.setStretchFactor(0, 0)
            self.splitter.setStretchFactor(1, 1)
            self._fit_header()
            return
        if self._tabs is None:
            first = self._bottom
            self._tabs = QTabWidget()
            self._tabs.setObjectName("bottom_tabs")
            self.splitter.replaceWidget(self.splitter.indexOf(first), self._tabs)
            self._tabs.addTab(first, self._bottom_icon, self._bottom_label)
            self._bottom = self._tabs
            self._tabs.currentChanged.connect(self._tab_changed)
        self._tabs.addTab(page, tab_icon, label)

    def _show_gallery_tabs(self, galleries: Sequence[DetailSection]) -> None:
        """The galleries as the last tabs below the sections (#456), made again on each
        refresh; the tab shown stays."""
        current = self._current_key()
        restoring = self._choosing
        self._choosing = True  # not the user's choice: don't remember it
        try:
            for old in self._gallery_tabs:
                if self._tabs is not None:
                    self._tabs.removeTab(self._tabs.indexOf(old))
                self._tab_keys.pop(id(old), None)
                old.deleteLater()
            self._gallery_tabs = []
            for section in galleries:
                tab = QWidget()
                tab.setObjectName(f"gallery_tab_{section.title}")
                column = QVBoxLayout(tab)
                column.setContentsMargins(0, 0, 0, 0)
                chips = KindChips()  # above the pictures, so they stay in view (#514)
                column.addWidget(chips)
                area = QScrollArea()
                area.setWidgetResizable(True)
                area.setFrameShape(QFrame.Shape.NoFrame)
                gallery = self._gallery(section)
                area.setWidget(gallery)
                column.addWidget(area, 1)
                count = len(section.files)
                label = f"{section.title} ({count:,})" if count else section.title
                self._embed(tab, label, f"gallery:{section.title}", lucide("images"))
                self._gallery_tabs.append(tab)
                self._gallery_chips(tab, section, gallery, chips)
            if current is not None:
                self._select_key(current)
        finally:
            self._choosing = restoring

    def _gallery_chips(
        self, tab: QWidget, section: DetailSection, gallery: GalleryView, chips: KindChips
    ) -> None:
        """A gallery tab's kind chips (#514): hide a gallery's files of some types, as the
        list tabs' chips do, remembered per item type and gallery."""
        if self.detail is None:
            return
        key = f"gallery:{self.detail.type}:{section.title}"
        counts = gallery.kinds()
        labels = {k: kind_label(k, {}) for k in counts}
        tips = {
            k: (kind_tip(k, labels[k], {}, True), kind_tip(k, labels[k], {}, False)) for k in counts
        }
        chips.set_hidden(self.recall_kinds(key))
        chips.set_kinds(counts, labels, tips)

        def show(hidden: list[str] | tuple[str, ...]) -> None:
            shown = gallery.set_hidden(hidden)
            total = len(section.files)
            if self._tabs is None or (index := self._tabs.indexOf(tab)) < 0:
                return
            if shown != total:
                text = f"{section.title} ({shown:,} of {total:,})"
            else:
                text = f"{section.title} ({total:,})" if total else section.title
            self._tabs.setTabText(index, text)

        def changed(hidden: list[str]) -> None:
            self.remember_kinds(key, hidden)
            show(hidden)

        chips.changed.connect(changed)
        if chips.hidden:
            show(chips.hidden)

    def _current_key(self) -> str | None:
        if self._tabs is None:
            return None
        current = self._tabs.currentWidget()
        return self._tab_keys.get(id(current)) if current is not None else None

    def _select_key(self, key: str) -> bool:
        """Show the tab of ``key`` if it is there and visible."""
        if self._tabs is None:
            return False
        for index in range(self._tabs.count()):
            widget = self._tabs.widget(index)
            if self._tab_keys.get(id(widget)) == key and self._tabs.isTabVisible(index):
                self._tabs.setCurrentIndex(index)
                return True
        return False

    _choosing = False

    def _choose_tab(self) -> None:
        """Show the tab last used on a page of this type, once it is there (#456)."""
        if self._tabs is None or self.detail is None:
            return
        wanted = self.recall_tab(self.detail.type)
        if wanted is None or wanted == self._current_key():
            return
        self._choosing = True
        try:
            self._select_key(wanted)
        finally:
            self._choosing = False

    def _tab_changed(self, index: int) -> None:
        if self._choosing or self.detail is None or self._tabs is None or index < 0:
            return
        key = self._current_key()
        if key is not None and self._tabs.isVisible():
            self.remember_tab(self.detail.type, key)
        self.selection_changed.emit()

    def embedded(self) -> list[SearchPage]:
        """The searches below the sections: contents, then related sections."""
        return list(self.contents_pages.values()) + list(self.related.values())

    def _show_breadcrumbs(self, detail: EntityDetail) -> None:
        """The containers above the entity, each a link to its page."""
        crumbs = [f'<a href="{c.id}">{html.escape(c.title)}</a>' for c in detail.breadcrumbs]
        text = f" {CRUMB} ".join(crumbs)
        if crumbs:
            text += f" {CRUMB}"
        if detail.other_parents:
            places = "1 other" if detail.other_parents == 1 else f"{detail.other_parents} others"
            text += f" <span style='color:{dimmed_css()}'>(also in {places})</span>"
        self.breadcrumbs.setText(text)
        self.breadcrumbs.setVisible(bool(crumbs))

    # --- the header's note ---

    def _show_note(self, loaded: _Loaded) -> None:
        detail = loaded.detail
        if detail is None:
            return
        has_file = detail.note_file is not None
        glimpsed = bool(loaded.glimpse)  # the item's own Markdown file's, without a note
        self.note_view.show_note(
            loaded.glimpse if glimpsed else detail.note_body,
            loaded.note_folder,
            loaded.pictures,
            has_file or glimpsed,
            editable=True,
            links=loaded.links,
            glimpse=glimpsed,
        )
        self.open_note_action.setVisible(has_file)
        config = self.session.keep.config
        self.save_note_action.setVisible(
            not has_file and config.notes_root is not None and config.notes_writable
        )
        # The note fills the room beside the picture; Show more gives it the rest.
        others = sum(
            w.sizeHint().height() + self._titles.spacing()
            for w in (self.merged_note, self.breadcrumbs, self.title_value, self.type_label)
            if w.isVisibleTo(self)
        )
        self.note_view.set_cap(HEADER_THUMBNAIL - others - self.note_view.more.sizeHint().height())

    def open_note(self) -> None:
        """Open the item's note file with its program; or, showing a glimpse of its own
        Markdown file (#554), that file."""
        detail = self.detail
        if self.file_opener is None or detail is None:
            return
        file = glimpsed_file(detail) if self.note_view.glimpse else detail.note_file
        if file is None:
            return
        opener, resource_id = self.file_opener, file.resource_id
        opener.lookup(self.entity_id, resource_id, lambda f: opener.act(f, OPEN))

    # --- the header's thumbnail ---

    def _show_thumbnail(self) -> None:
        if self.thumbnails is None:
            return
        loaded = self.thumbnails.get(self.entity_id)
        if loaded is None:
            return  # requested: _thumbnail_ready shows it
        if loaded.image is not None:
            self.thumbnail.setPixmap(fitted(loaded.image, HEADER_THUMBNAIL))
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
        return self._titled(title, body, f"section_{section.kind}")

    def _keywords(self, detail: EntityDetail) -> QWidget:
        """Each keyword its files give, with the tag it gives and what can be done (#295)."""
        tags = dict(detail.keyword_tags)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(12, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        for row, item in enumerate(detail.keywords):
            grid.addWidget(QLabel(item.keyword), row, 0)
            path = tags.get(item.tag_id, "") if item.tag_id is not None else ""
            text, buttons = {
                "tagged": (f"\u2192 {path}", []),
                "removed": (f"\u2192 {path} (removed from this item)", ["Restore"]),
                "not allowed": (f"\u2192 {path} (not for this type)", []),
                "ignored": ("Ignored", ["Stop ignoring"]),
                "unmatched": ("No tag", ["Map\u2026", "Ignore"]),
            }[item.state]
            state = QLabel(text)
            state.setObjectName(f"keyword_{item.key}")
            grid.addWidget(state, row, 1)
            actions = QHBoxLayout()
            for name in buttons:
                button = QPushButton(name)
                button.setFlat(True)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(
                    lambda _=False, name=name, item=item: self._keyword_action(name, item)
                )
                actions.addWidget(button)
            actions.addStretch(1)
            grid.addLayout(actions, row, 2)
        grid.setColumnStretch(2, 1)
        return body

    def _keyword_action(self, name: str, item: ItemKeyword) -> None:
        if name == "Restore" and item.tag_id is not None:
            self.file_tag_restore_requested.emit(self.entity_id, item.tag_id)
        elif name.startswith("Map"):
            self.keyword_map_requested.emit(KeywordInfo(item.key, item.keyword, 1, (), None, False))
        elif name in ("Ignore", "Stop ignoring"):
            self.keyword_ignore_requested.emit([item.key], name == "Ignore")

    def _titled(self, title: str, body: QWidget, name: str) -> QWidget:
        """A section: a bold heading with a rule, then its body."""
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
        box.setObjectName(name)
        return box

    def _extra_widget(self, detail: EntityDetail) -> QWidget:
        """The user's own fields: each editable in place, with x to remove it, and a row
        to add one."""
        body = QWidget()
        form = _form(body)
        entity_id = self.entity_id
        sources = dict(detail.extra_sources)
        for name, value in detail.extra:
            row = QHBoxLayout()
            source = sources.get(name)
            editor = EditableValue(value, str, editable=True, edited=source is not None, label=name)
            if source == FieldSource.NOTE:
                editor.marker.setText("\u2022 from the note")
                editor.marker.setToolTip(FROM_NOTE_TIP)
            elif source == FieldSource.EXTRACTED:  # its own front matter (#410)
                editor.marker.setText("\u2022 from the file")
                editor.marker.setToolTip(FROM_FILE_TIP)
            given = source in (FieldSource.NOTE, FieldSource.EXTRACTED)
            editor.setObjectName(f"extra_{name}")
            editor.committed.connect(
                lambda new, name=name: self.extra_edited.emit(entity_id, name, new, False)
            )
            remove = QToolButton()
            remove.setText(CLOSE_MARK)
            remove.setAutoRaise(True)
            remove.setToolTip(
                f"Remove {name!r}; scans won't add it back"
                if source is not None
                else f"Remove {name!r}"
            )
            remove.clicked.connect(
                lambda _c=False, name=name: self.extra_edited.emit(entity_id, name, None, False)
            )
            # The arrow: make it a field (#582); and for a note's or file's key, that
            # key in every note and file (#381, #410).
            remove.setObjectName(f"remove_{name}")
            remove.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            menu = QMenu(remove)
            menu.addAction("Remove from this item", remove.click)
            make = menu.addAction(MAKE_FIELD)
            make.setToolTip(
                f"Make {name!r} a field of your own, for this item and every other with it"
            )
            make.triggered.connect(
                lambda _c=False, name=name: self.promote_requested.emit(name, detail.type)
            )
            remove.setMenu(menu)
            if given:
                ignore = menu.addAction(IGNORE_KEY)
                ignore.setToolTip(f"Leave {name!r} out of every note and file in this keep")
                ignore.triggered.connect(lambda _c=False, name=name: self.ignore_note_key(name))
                as_field = menu.addAction(ALIAS_KEY)
                as_field.setToolTip(
                    f"Read {name!r} as one of this type's fields in every note and file"
                )
                as_field.triggered.connect(
                    lambda _c=False, name=name: self.alias_note_key(name, detail.type)
                )
            row.addWidget(editor)
            row.addWidget(remove)
            row.addStretch(1)  # the x follows its value
            form.addRow(f"{name}:", row)
        self.extra_name = QLineEdit()
        self.extra_name.setPlaceholderText("Field name")
        self.extra_value = QLineEdit()
        self.extra_value.setPlaceholderText("Value")
        add = QPushButton("Add")
        add.setToolTip("Add a field of your own to this item (it's searchable as text)")
        for widget in (self.extra_name, self.extra_value):
            widget.returnPressed.connect(self._add_extra)
        add.clicked.connect(self._add_extra)
        adding = QHBoxLayout()
        adding.addWidget(self.extra_name, 1)
        adding.addWidget(self.extra_value, 2)
        adding.addWidget(add)
        form.addRow(adding)
        return self._titled("Extra fields", body, "section_extra")

    def ignore_note_key(self, name: str) -> None:
        """Ignore ``name`` in every note of the keep, if the user agrees."""
        if self.confirm_ignore_key(name):
            self.note_key_ignore_requested.emit(name)

    def alias_note_key(self, name: str, type_id: str) -> None:
        """Ask which of the type's fields ``name`` fills, then ask for the alias (#411)."""
        fields = self.session.schema.by_type_id(type_id).fields
        labels = {str(f.spec.label or f.name): f.name for f in fields}
        chosen = self.choose_alias_field(name, list(labels))
        if chosen is not None and chosen in labels:
            self.note_key_alias_requested.emit(name, labels[chosen], chosen)

    def choose_alias_field(self, name: str, labels: list[str]) -> str | None:
        """The field label the user chose for ``name``, or ``None``."""
        label, ok = QInputDialog.getItem(
            self,
            "Read this key as a field",
            f"In this keep's notes and Markdown files, read {name!r} as:\n\n"
            "From the next scan (F5); a file's own key for the field wins. Keep "
            "configuration \u2192 Notes lists the keys read as fields.",
            labels,
            0,
            False,
        )
        return label if ok else None

    def confirm_ignore_key(self, name: str) -> bool:
        answer = QMessageBox.question(
            self,
            "Ignore this key in every note?",
            f"Leave {name!r} out of every note in this keep?\n\n"
            f"The {name!r} fields that notes and files' front matter gave are removed from "
            "every item now, and "
            "scans leave the key out from then on. Fields you added or edited stay. "
            "Edit → Undo puts them back; Keep configuration → Notes lists the keys "
            "ignored.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _add_extra(self) -> None:
        name, value = self.extra_name.text().strip(), self.extra_value.text().strip()
        if not name:
            self.extra_name.setFocus()
            return
        if not value:
            self.extra_value.setFocus()
            return
        self.extra_edited.emit(self.entity_id, name, value, True)

    def _fields(self, section: DetailSection) -> QWidget:
        body = QWidget()
        form = _form(body)
        for row in section.fields:
            value = EditableValue(
                row.value,
                row.type,
                editable=row.editable,
                edited=row.edited,
                label=row.label,
                display=row.display,
            )
            value.setObjectName(f"field_{row.name}")
            if row.hint:  # it follows another container (#440)
                value.setToolTip(row.hint)
            value.committed.connect(
                lambda new, name=row.name: self.field_edited.emit(self.entity_id, name, new)
            )
            if row.name.startswith(OWN):  # one of the keep's own (#582)
                form.addRow(f"{row.label}:", self._own_row(value, row.name))
                continue
            form.addRow(f"{row.label}:", value)
        return body

    def _own_row(self, value: EditableValue, name: str) -> QHBoxLayout:
        """An own field's value with a menu: turn it back into Extra fields."""
        more = QToolButton()
        more.setObjectName(f"more_{name}")
        more.setText("\u25be")
        more.setAutoRaise(True)
        more.setToolTip("A field of your own (Keep configuration \u2192 Fields)")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        back = menu.addAction(TURN_BACK)
        back.setToolTip("Its values become Extra fields again, in every item")
        back.triggered.connect(
            lambda _c=False: self.turn_back_requested.emit(self._type_id(), name)
        )
        more.setMenu(menu)
        row = QHBoxLayout()
        row.addWidget(value)
        row.addWidget(more)
        row.addStretch(1)
        return row

    def _type_id(self) -> str:
        return self.detail.type if self.detail is not None else ""

    def _show_action_buttons(self, detail: EntityDetail) -> None:
        """A button for each theme action that applies to the item (once)."""
        if self._action_buttons.count():
            return
        for theme_action in actions_for(self.session.schema, detail.type):
            button = QPushButton(theme_action.label)
            button.setObjectName(f"action_{theme_action.method}")
            button.clicked.connect(
                lambda _=False, m=theme_action.method: self.action_requested.emit(
                    m, [self.entity_id]
                )
            )
            self._action_buttons.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)

    def _add_write_back(self, detail: EntityDetail) -> None:
        """Write to file… at the end of More (once): any item's Markdown file or note (#501)."""
        if self._write_back_added:
            return
        self._write_back_added = True
        write = self._more_menu.addAction("Write to file\u2026")
        write.setObjectName("write_back")
        write.setToolTip(WRITE_BACK_TIP)
        write.triggered.connect(lambda: self.write_back_requested.emit([self.entity_id]))

    def _add_look_up(self) -> None:
        """Look up online at the end of More (once), for themes that look things up."""
        if self._look_up_added or not self.session.theme.online_sources:
            return
        self._look_up_added = True
        look_up = self._more_menu.addAction("Look up online")
        look_up.setObjectName("look_up")
        look_up.setToolTip(LOOK_UP_TIP)
        look_up.triggered.connect(lambda: self.look_up_requested.emit([self.entity_id]))

    def _add_file_actions(self, detail: EntityDetail) -> None:
        """Open file, Show in file manager, Open with… at the top of More (once)."""
        kind = file_kind(self.session.schema, detail.type)
        if self._file_actions_added or kind is None or self.file_opener is None:
            return
        self._file_actions_added = True
        first = self._more_menu.actions()[0] if self._more_menu.actions() else None
        added = QMenu(self._more_menu)
        add_file_actions(added, self.file_opener, entity_id=self.entity_id, folder=kind == "folder")
        for action in [*added.actions(), added.addSeparator()]:
            self._more_menu.insertAction(first, action)  # type: ignore[arg-type]

    def _files(self, section: DetailSection) -> QWidget:
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(12, 0, 0, 0)
        column.setSpacing(2)
        for file in section.files:
            label = _file_label(file)
            label.installEventFilter(self)
            label.setProperty("resource_id", file.resource_id)
            label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            label.customContextMenuRequested.connect(
                lambda point, f=file, w=label: self._file_menu(f).exec(w.mapToGlobal(point))
            )
            column.addWidget(label)
        if not section.files:
            column.addWidget(QLabel("None"))
        return body

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Double-clicking a file row opens that file."""
        if event.type() == QEvent.Type.MouseButtonDblClick and self.file_opener is not None:
            resource_id = watched.property("resource_id")
            if isinstance(resource_id, int):
                self._open_file(resource_id)
                return True
        return super().eventFilter(watched, event)

    def _open_file(self, resource_id: object) -> None:
        """Open one of the item's files with its program."""
        if self.file_opener is not None and isinstance(resource_id, int):
            opener = self.file_opener
            opener.lookup(self.entity_id, resource_id, lambda f: opener.act(f, OPEN))

    def _show_picture_source(self, detail: EntityDetail) -> None:
        """Say where the item's picture comes from, when it's its own (#439)."""
        picture = detail.picture
        mine = picture is not None and picture.source == USER
        self.usual_picture_action.setVisible(mine)
        if picture is None:
            self.thumbnail.setToolTip("")
            return
        name = picture.relpath.rpartition("/")[2]
        how = "chosen by you" if mine else "from its note"
        self.thumbnail.setToolTip(f"Its picture, {how}: {name}")

    def _file_menu(self, file: FileRow) -> QMenu:
        """The right-click menu of one file row."""
        menu = QMenu(self)
        if is_picture(file.relpath) and file.kind != "dir":
            use = menu.addAction("Use as its picture")
            use.setToolTip("Show this picture for the item wherever it is (Edit \u2192 Undo)")
            use.triggered.connect(
                lambda: self.picture_requested.emit(self.entity_id, file.resource_id)
            )
            menu.addSeparator()
        if self.file_opener is not None:
            add_file_actions(
                menu,
                self.file_opener,
                entity_id=self.entity_id,
                resource_id=file.resource_id,
                folder=file.kind == "dir",
                inside_archive=bool(file.archive),
            )
        if self.archive_actions is not None:
            self._add_archive_actions(menu, file)
        if file.place_of is not None:  # another place of the file above (#600)
            menu.addSeparator()
            split = menu.addAction("Unlink this place")
            split.setToolTip(
                "It isn't the same file: make it a file of its own, with its own item "
                "(Edit \u2192 Undo)"
            )
            split.triggered.connect(lambda: self.place_unlink_requested.emit(file.resource_id))
        if file.by_user and file.role is not None:
            menu.addSeparator()
            unlink = menu.addAction("Unlink from this item")
            unlink.setToolTip("You linked this file here; remove the link (Edit → Undo)")
            role = file.role
            unlink.triggered.connect(
                lambda: self.unlink_requested.emit(self.entity_id, file.resource_id, role)
            )
        return menu

    def _add_archive_actions(self, menu: QMenu, file: FileRow) -> None:
        """What to read inside an archive (#519): an archive's commands, Hide for a file
        inside one, and a folder's archives."""
        actions = self.archive_actions
        assert actions is not None
        if file.archive:
            menu.addSeparator()
            actions.add_hide(menu, file.resource_id)
        elif file.kind == "file" and is_archive(file.relpath):
            menu.addSeparator()
            actions.add_archive_actions(menu, file.resource_id)
        elif file.kind == "dir" and file.root_id:
            menu.addSeparator()
            actions.add_folder_action(menu, file.root_id, file.relpath)

    def _gallery(self, section: DetailSection) -> GalleryView:
        """The role's pictures in groups, archives' pictures included (#386)."""
        gallery = GalleryView(
            section, self.session, self.entity_id, self.file_opener, self._file_menu, self._pool
        )
        gallery.archive_actions = self.archive_actions
        return gallery

    def _links(self, section: DetailSection) -> QWidget:
        """The related items as links, each with an x to remove it, and Add… (#260)."""
        name = section.relationship
        remove = (
            f' <a href="x:{{id}}" style="text-decoration: none; color: {dimmed_css()}">'
            f"{CLOSE_MARK}</a>"
            if name
            else ""
        )
        links = " \u00b7 ".join(
            f'<a href="{e.id}">{html.escape(e.title)}</a>' + remove.format(id=e.id)
            for e in section.entities
        )
        label = QLabel(links or "None")
        label.setObjectName(f"related_{name}")
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.linkActivated.connect(lambda href: self._related_link(name, href, section.side or ""))
        label.linkHovered.connect(
            lambda href: label.setToolTip(
                f"Remove from {section.title.lower()} (Edit \u2192 Undo brings it back)"
                if href.startswith("x:")
                else ""
            )
        )
        if not name:
            label.setContentsMargins(12, 0, 0, 0)
            return label
        add = QPushButton("Add\u2026")
        add.setObjectName(f"add_{name}")
        add.setFlat(True)
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.setToolTip(f"Add to {section.title.lower()} by hand; scans won't remove it")
        add.clicked.connect(
            lambda: self.relate_requested.emit(self.entity_id, name, section.side or "")
        )
        body = QWidget()
        row = QHBoxLayout(body)
        row.setContentsMargins(12, 0, 0, 0)
        row.addWidget(label, 1)
        row.addWidget(add, 0, Qt.AlignmentFlag.AlignTop)
        return body

    def _related_link(self, name: str | None, href: str, side: str = "") -> None:
        if href.startswith("x:"):
            if name:
                self.unrelate_requested.emit(self.entity_id, name, [int(href[2:])], side)
        else:
            self.open_entity.emit(int(href))

    # --- like a search page ---

    def clear_selection(self) -> None:
        """Select nothing in the contents shown, so tagging is the page's item's again."""
        shown = self._tabs.currentWidget() if self._tabs is not None else self._bottom
        if isinstance(shown, SearchPage):
            shown.clear_selection()

    def selected_entity_ids(self, on_done: Callable[[list[int]], None]) -> None:
        """What tagging applies to: the items selected in the contents, else the page's
        own entity."""
        own = [self.entity_id] if self.detail is not None else []
        shown = self._tabs.currentWidget() if self._tabs is not None else self._bottom
        if not isinstance(shown, SearchPage):
            on_done(own)
            return
        shown.selected_entity_ids(lambda ids: on_done(ids or own))


def _form(body: QWidget) -> QFormLayout:
    """A section's fields: each label above its value, as the Details tab is narrow (#468)."""
    form = QFormLayout(body)
    form.setContentsMargins(12, 0, 0, 0)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
    return form


def _file_label(file: FileRow) -> QLabel:
    text = html.escape(file.shown or "(the root folder)")
    if file.place_of is not None:  # another place of the file above it (#598)
        text = f"Also at {text}"
    facts = [html.escape(file.root_name)]
    if file.size is not None and file.kind == "file" and file.place_of is None:
        facts.append(f"{file.size:,} bytes")
    if file.status is ResourceStatus.OFFLINE:
        facts.append("<b>offline</b>")
    elif file.status is ResourceStatus.MISSING:
        facts.append(f'<b style="color:{css(ERROR)}">missing</b>')
    if file.skipped:
        facts.append("<b>skipped by the folder's settings</b>")
    if file.by_user:
        facts.append("linked by you")
    label = QLabel(f"{text} <span style='color:{dimmed_css()}'>· {' · '.join(facts)}</span>")
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setWordWrap(True)  # long paths wrap rather than widen the page
    if file.place_of is not None:
        label.setIndent(16)
    if file.path is None:
        label.setToolTip("This root has no path on this computer")
    elif file.archive:  # opened from a copy; the archive is never changed (#524)
        label.setToolTip(f"Inside the archive {file.path}")
    else:
        label.setToolTip(file.path)
    return label


def _root_path(session: KeepSession) -> Callable[[str], str | None]:
    def path(root_id: str) -> str | None:
        try:
            return session.root_path(root_id)
        except StopIteration:  # a root no longer in keep.toml
            return None

    return path
