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
from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QFont, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.actions import actions_for
from tagalot.core.containers import hand_made_types
from tagalot.core.detail import DetailSection, EntityDetail, FileRow, load_detail
from tagalot.core.handlers import OPEN
from tagalot.core.ingest import TITLE
from tagalot.core.keywords import ItemKeyword, KeywordInfo
from tagalot.core.models import ResourceStatus
from tagalot.core.search import POSITION
from tagalot.core.search_fields import contained_types, contents_order
from tagalot.core.search_spec import SearchSpec, SortKey
from tagalot.core.session import KeepSession
from tagalot.ui.field_editor import EditableValue
from tagalot.ui.field_filters import CLOSE_MARK
from tagalot.ui.file_actions import FileOpener, add_file_actions, file_kind
from tagalot.ui.lookups import LOOK_UP_TIP
from tagalot.ui.search_view import WRITE_BACK_TIP, SearchPage, add_reread_actions, writes_back
from tagalot.ui.thumbnails import ThumbnailLoader, icon_for
from tagalot.ui.workers import run_in_pool

HEADER_THUMBNAIL = 192
"""The header thumbnail's box, in pixels."""

GALLERY_THUMBNAIL = 128

CRUMB = "\u203a"
"""Between breadcrumbs."""


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
    reread_requested = Signal(list, bool)
    write_back_requested = Signal(list)
    look_up_requested = Signal(list)
    """Look up online (#340): the item's id, in a list."""
    """Write to file… on this item (#299)."""
    unlink_requested = Signal(int, int, str)
    """Remove a link made by hand: (entity id, resource id, role)."""
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
        self.contents: SearchPage | None = None
        """The search of a container's contents, once the page knows it is one."""
        self.related: dict[str, SearchPage] = {}
        """Each related section's search, by relationship name (#125)."""
        self._bottom: QWidget | None = None
        """What sits below the sections: one embedded search, or tabs of several."""
        self._tabs: QTabWidget | None = None
        self._bottom_label = ""
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
        self.merged_note = QLabel("The item opened here was merged into this one.")
        self.merged_note.setObjectName("merged_note")
        self.merged_note.setStyleSheet("color: palette(placeholder-text); font-style: italic;")
        self.merged_note.setVisible(False)
        titles = QVBoxLayout()
        titles.addWidget(self.merged_note)
        titles.addWidget(self.breadcrumbs)
        titles.addWidget(self.title_value)
        titles.addWidget(self.type_label)
        titles.addStretch(1)
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
        """Opens the item's files (the window sets it); without one, no file actions."""
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
        generation, session, entity_id = self._generation, self.session, self.requested_id

        def job() -> EntityDetail | None:
            with session.reader.connect() as conn:
                return load_detail(conn, session.schema, entity_id, _root_path(session))

        def done(detail: EntityDetail | None) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._show(detail)

        run_in_pool(job, on_done=done, pool=self._pool)

    def _show(self, detail: EntityDetail | None) -> None:
        # While the item asked for is merged into another (§13), the page is that one's;
        # after an undo it is the item's own again.
        merged = detail is not None and detail.merged_from is not None
        self.entity_id = detail.id if detail is not None else self.requested_id
        self.merged_note.setVisible(merged)
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
            self._add_file_actions(detail)
            self._add_write_back(detail)
            self._add_look_up()
            self._show_action_buttons(detail)
            self._show_breadcrumbs(detail)
            extras = self._extra_widget(detail)
            for section in detail.sections:
                if section.kind == "contents" and self._show_contents(detail):
                    continue
                if section.kind == "related" and self._show_related(section):
                    continue
                widget = self._section_widget(section)
                if widget is not None:
                    self._sections.addWidget(widget)
                if section.kind == "fields" and extras.parent() is None:
                    self._sections.addWidget(extras)  # the user's fields follow the theme's
            if extras.parent() is None:
                self._sections.insertWidget(0, extras)
            if detail.keywords:
                self._sections.addWidget(
                    self._titled("From the file", self._keywords(detail), "section_keywords")
                )
            self._show_thumbnail()
        self.loaded.emit()

    def _show_contents(self, detail: EntityDetail) -> bool:
        """Put the search of the entity's contents below the sections (once). False when
        the window gave no way to make one: then the section shows a count."""
        if self._make_contents is None:
            return False
        if self.contents is None:
            types = tuple(contained_types(self.session.schema, detail.type))
            sort, _ = contents_order(self.session.schema, detail.type)
            spec = SearchSpec(types=types, within=self.requested_id, sort=sort)
            self.contents = self._make_contents(f"contents:{detail.type}", spec, "Contents")
            self.contents.selection_changed.connect(self.selection_changed)
            in_search = QPushButton("Show in search")
            in_search.setFlat(True)
            in_search.setCursor(Qt.CursorShape.PointingHandCursor)
            in_search.setToolTip("Open Search all with a Within chip for these contents")
            in_search.clicked.connect(lambda: self.show_in_search.emit(self.entity_id))
            self.contents.header_row.insertWidget(1, in_search)
            if detail.type in hand_made_types(self.session.schema):  # a project (#329)
                self.contents.add_menu_action(
                    f"Remove from {detail.title}",
                    lambda ids: self.uncontain_requested.emit(self.entity_id, ids),
                )
            self._embed(self.contents, "Contents")
        return True

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
            self._embed(page, section.title)
        return True

    def _orders(self, name: str) -> bool:
        """Whether this page's item has its own order of relationship ``name``'s items (it
        is the ``b`` side of an ordered relationship: a paper, for its authors)."""
        link = self.session.schema.relationships.get(name)
        if link is None or not link.relationship.ordered or self.detail is None:
            return False
        return self.session.schema.theme.type_id_of(link.relationship.b) == self.detail.type

    def _embed(self, page: SearchPage, label: str) -> None:
        """Put an embedded search below the sections; with more than one, as tabs."""
        if self._bottom is None:
            self._bottom, self._bottom_label = page, label
            self.splitter.addWidget(page)
            self.splitter.setStretchFactor(0, 2)
            self.splitter.setStretchFactor(1, 3)
            return
        if self._tabs is None:
            first = self._bottom
            self._tabs = QTabWidget()
            self.splitter.replaceWidget(self.splitter.indexOf(first), self._tabs)
            self._tabs.addTab(first, self._bottom_label)
            self._bottom = self._tabs
        self._tabs.addTab(page, label)

    def embedded(self) -> list[SearchPage]:
        """The searches below the sections: contents, then related sections."""
        return ([self.contents] if self.contents is not None else []) + list(self.related.values())

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
        form = QFormLayout(body)
        form.setContentsMargins(12, 0, 0, 0)
        entity_id = self.entity_id
        for name, value in detail.extra:
            row = QHBoxLayout()
            editor = EditableValue(value, str, editable=True, label=name)
            editor.setObjectName(f"extra_{name}")
            editor.committed.connect(
                lambda new, name=name: self.extra_edited.emit(entity_id, name, new, False)
            )
            remove = QToolButton()
            remove.setText(CLOSE_MARK)
            remove.setAutoRaise(True)
            remove.setToolTip(f"Remove {name!r}")
            remove.clicked.connect(
                lambda _c=False, name=name: self.extra_edited.emit(entity_id, name, None, False)
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
        form = QFormLayout(body)
        form.setContentsMargins(12, 0, 0, 0)
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
            value.committed.connect(
                lambda new, name=row.name: self.field_edited.emit(self.entity_id, name, new)
            )
            form.addRow(f"{row.label}:", value)
        return body

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
        """Write to file… at the end of More (once), for types the theme writes back."""
        if self._write_back_added or not writes_back(self.session, detail.type):
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

    def _file_menu(self, file: FileRow) -> QMenu:
        """The right-click menu of one file row."""
        menu = QMenu(self)
        if self.file_opener is not None:
            add_file_actions(
                menu,
                self.file_opener,
                entity_id=self.entity_id,
                resource_id=file.resource_id,
                folder=file.kind == "dir",
            )
        if file.by_user and file.role is not None:
            menu.addSeparator()
            unlink = menu.addAction("Unlink from this item")
            unlink.setToolTip("You linked this file here; remove the link (Edit → Undo)")
            role = file.role
            unlink.triggered.connect(
                lambda: self.unlink_requested.emit(self.entity_id, file.resource_id, role)
            )
        return menu

    def _gallery(self, section: DetailSection) -> QWidget:
        gallery = QListWidget()
        gallery.setViewMode(QListView.ViewMode.IconMode)
        gallery.setIconSize(QSize(GALLERY_THUMBNAIL, GALLERY_THUMBNAIL))
        gallery.setResizeMode(QListView.ResizeMode.Adjust)
        gallery.setMovement(QListView.Movement.Static)
        gallery.setWrapping(True)
        gallery.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        gallery.setMinimumHeight(GALLERY_THUMBNAIL + 48)
        gallery.setObjectName(f"gallery_{section.title}")
        items: dict[int, QListWidgetItem] = {}
        files: dict[int, FileRow] = {}
        for file in section.files:
            item = QListWidgetItem(icon_for("image"), file.relpath.rpartition("/")[2])
            item.setToolTip(file.relpath)
            item.setData(Qt.ItemDataRole.UserRole, file.resource_id)
            gallery.addItem(item)
            items[file.resource_id] = item
            files[file.resource_id] = file
        # Like a file row: double-click (or Enter) opens the picture, right-click its menu.
        gallery.itemActivated.connect(
            lambda item: self._open_file(item.data(Qt.ItemDataRole.UserRole))
        )
        gallery.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        gallery.customContextMenuRequested.connect(
            lambda point: self._gallery_menu(gallery, files, point)
        )
        if section.files:
            self._load_gallery(gallery, items)
        return gallery

    def _gallery_menu(self, gallery: QListWidget, files: dict[int, FileRow], point: QPoint) -> None:
        item = gallery.itemAt(point)
        file = files.get(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None
        if file is not None:
            self._file_menu(file).exec(gallery.viewport().mapToGlobal(point))

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
        """The related items as links, each with an x to remove it, and Add… (#260)."""
        name = section.relationship
        remove = (
            f' <a href="x:{{id}}" style="text-decoration: none; color: gray">{CLOSE_MARK}</a>'
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

    def selected_entity_ids(self, on_done: Callable[[list[int]], None]) -> None:
        """What tagging applies to: the items selected in the contents, else the page's
        own entity."""
        own = [self.entity_id] if self.detail is not None else []
        shown = self._tabs.currentWidget() if self._tabs is not None else self._bottom
        if not isinstance(shown, SearchPage):
            on_done(own)
            return
        shown.selected_entity_ids(lambda ids: on_done(ids or own))


def _file_label(file: FileRow) -> QLabel:
    text = html.escape(file.relpath or "(the root folder)")
    facts = [html.escape(file.root_name)]
    if file.size is not None and file.kind == "file":
        facts.append(f"{file.size:,} bytes")
    if file.status is ResourceStatus.OFFLINE:
        facts.append("<b>offline</b>")
    elif file.status is ResourceStatus.MISSING:
        facts.append('<b style="color:#c0392b">missing</b>')
    if file.skipped:
        facts.append("<b>skipped by the folder's settings</b>")
    if file.by_user:
        facts.append("linked by you")
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
