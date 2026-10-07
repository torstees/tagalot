"""The dashboard: the keep at a glance (DESIGN.md §12 "Dashboard").

Cards in a grid, each number a link:

- **Items:** how many of each type; a type opens Search all narrowed to it.
- **Untagged:** how many items have no tag of their own (Triage's list), as a share with a
  bar; it opens Triage.
- **Folders:** each root's state and last scan; a folder opens Keep configuration on it.
- **Recently added:** the newest items' thumbnails; double-click opens a page.
- **Most used tags** and **Least used tags** (unused ones first): a tag opens a search
  with it.
- **The theme's cards** (``Theme.dashboard`` and ``@dashboard_card`` methods): statistics
  ("Total running time"), most common values (a value opens a search of those items), or
  rows a theme method computed; a card that failed shows why.

It is read in a worker (``core.dashboard``) when shown, and again after a scan, tagging, or
a configuration change.
"""

import html
from datetime import UTC, datetime

import shiboken6
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.dashboard import CardRow, Dashboard, TagUse, load_dashboard
from tagalot.core.search_spec import FieldFilter
from tagalot.core.session import KeepSession
from tagalot.ui.empty_state import EmptyState
from tagalot.ui.thumbnails import ThumbnailLoader, icon_for
from tagalot.ui.workers import run_in_pool

THUMB = 96
_ID = Qt.ItemDataRole.UserRole


class DashboardPage(QWidget):
    """See the module docstring. Signals ask the window to go somewhere."""

    open_type = Signal(str)
    open_triage = Signal()
    configure_root = Signal(str)
    open_entity = Signal(int)
    search_tag = Signal(int)
    search_value = Signal(str, object)
    """(type id, field filter): items of that type with a theme card's value."""
    configure_requested = Signal()
    scan_requested = Signal()

    def __init__(
        self, session: KeepSession, thumbnails: ThumbnailLoader, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.thumbnails = thumbnails
        self.data: Dashboard | None = None
        self._generation = 0

        self.title = QLabel()
        self.title.setStyleSheet("font-size: 16pt; font-weight: 600;")
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        header = QHBoxLayout()
        header.addWidget(self.title, 1)
        header.addWidget(refresh)

        self.items = _links(self._clicked)
        self.untagged = _links(self._clicked)
        self.untagged_bar = QProgressBar()
        self.untagged_bar.setTextVisible(False)
        self.untagged_bar.setMaximumHeight(8)
        self.folders = _links(self._clicked)
        self.recent = QListWidget()
        self.recent.setObjectName("recent")
        self.recent.setViewMode(QListView.ViewMode.IconMode)
        self.recent.setFlow(QListView.Flow.LeftToRight)
        self.recent.setWrapping(False)
        self.recent.setMovement(QListView.Movement.Static)
        self.recent.setIconSize(QSize(THUMB, THUMB))
        self.recent.setFixedHeight(THUMB + 56)
        self.recent.setWordWrap(True)
        self.recent.itemActivated.connect(lambda item: self.open_entity.emit(item.data(_ID)))
        self.most_used = _links(self._clicked)
        self.least_used = _links(self._clicked)
        self.thumbnails.ready.connect(self._thumbnail_ready)

        grid = QGridLayout()
        grid.setSpacing(12)
        grid.addWidget(_card("Items", self.items), 0, 0)
        grid.addWidget(_card("Untagged", self.untagged, self.untagged_bar), 0, 1)
        grid.addWidget(_card("Folders", self.folders), 0, 2)
        grid.addWidget(_card("Recently added", self.recent), 1, 0, 1, 3)
        grid.addWidget(_card("Most used tags", self.most_used), 2, 0)
        grid.addWidget(_card("Least used tags", self.least_used), 2, 1)
        for n in range(3):
            grid.setColumnStretch(n, 1)
        self.theme_grid = QGridLayout()
        self.theme_grid.setSpacing(12)
        for n in range(3):
            self.theme_grid.setColumnStretch(n, 1)
        self._values: list[tuple[str, FieldFilter]] = []
        self.cards = QWidget()
        cards = QVBoxLayout(self.cards)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.addLayout(grid)
        cards.addLayout(self.theme_grid)
        self.empty = EmptyState(
            "This keep is empty",
            "Add a folder for it to watch, then scan it: its files become items here.",
        )
        self.empty.add_button("Configure keep\u2026", self.configure_requested.emit)
        self.empty.add_button("Scan now", self.scan_requested.emit)
        self.empty.setVisible(False)
        body = QWidget()
        column = QVBoxLayout(body)
        column.addLayout(header)
        column.addWidget(self.cards)
        column.addWidget(self.empty, 1)
        column.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
        self.refresh()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self.data is not None:  # coming back to it: it may be out of date
            self.refresh()

    def refresh(self) -> None:
        """Read everything again, in a worker."""
        self._generation += 1
        generation, session = self._generation, self.session

        def job() -> Dashboard:
            tree = session.tag_cache.get()
            with session.reader.connect() as conn:
                return load_dashboard(conn, session.schema, tree)

        def done(data: Dashboard) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._show(data)

        run_in_pool(job, on_done=done)

    # --- showing ---

    def _show(self, data: Dashboard) -> None:
        self.data = data
        self.title.setText(html.escape(self.session.keep.config.name))
        self.cards.setVisible(data.total > 0)
        self.empty.setVisible(data.total == 0)  # nothing yet: the knight says how (#360)
        rows = [
            f'<a href="type:{t.type_id}">{html.escape(t.plural)}</a>: {t.count:,}'
            for t in data.types
        ]
        self.items.setText("<br>".join(rows) + f"<br><b>{data.total:,} in all</b>")
        share = round(data.untagged_share * 100)
        self.untagged.setText(
            f"{data.untagged:,} of {data.total:,} items ({share}%) have no tag of their own."
            '<br><a href="triage:">Tag them in Triage →</a>'
            if data.untagged
            else "Every item has a tag."
        )
        self.untagged_bar.setRange(0, max(data.total, 1))
        self.untagged_bar.setValue(data.untagged)
        self.folders.setText(self._folders(data) or "No folders yet.")
        self.most_used.setText(_tags(data.most_used) or "No tags used yet.")
        self.least_used.setText(_tags(data.least_used) or "No tags yet.")
        self._show_theme_cards(data)
        self.recent.clear()
        for hit in data.recent:
            item = QListWidgetItem(icon_for("entity"), hit.title)
            item.setData(_ID, hit.id)
            item.setToolTip(hit.title)
            item.setSizeHint(QSize(THUMB + 24, THUMB + 48))
            self.recent.addItem(item)
            self._thumbnail_ready(hit.id)

    def _show_theme_cards(self, data: Dashboard) -> None:
        while self.theme_grid.count():
            item = self.theme_grid.takeAt(0)
            if item is not None and (widget := item.widget()) is not None:
                widget.deleteLater()
        self._values = []
        for n, card in enumerate(data.theme_cards):
            body = _links(self._clicked)
            if card.error is not None:
                body.setText(
                    f'<span style="color:#c0392b">Couldn\'t work this out: '
                    f"{html.escape(card.error)}</span>"
                )
            else:
                body.setText("<br>".join(self._row(row) for row in card.rows) or "\u2014")
            frame = _card(card.title, body)
            frame.setToolTip(card.description)
            self.theme_grid.addWidget(frame, n // 3, n % 3)

    def _row(self, row: CardRow) -> str:
        label = html.escape(row.label)
        if row.search is not None:
            self._values.append(row.search)
            label = f'<a href="value:{len(self._values) - 1}">{label}</a>'
        if not row.value:
            return f"<span style='font-size: 14pt'>{label}</span>" if not row.search else label
        return f"{label}: {html.escape(row.value)}"

    def _folders(self, data: Dashboard) -> str:
        rows = []
        for root in self.session.keep.config.roots:
            status = data.roots.get(root.id)
            if not root.watched:
                state = "not watched"
            elif status is None or status.last_scan_at is None:
                state = "not scanned yet"
            elif status.online:
                state = f"online, scanned {_ago(status.last_scan_at)}"
            else:
                state = '<span style="color:#c0392b">offline</span>'
            files = f", {status.files:,} files" if status is not None else ""
            rows.append(
                f'<a href="root:{html.escape(root.id)}">{html.escape(root.name)}</a>: '
                f"{state}{files}"
            )
        return "<br>".join(rows)

    def _thumbnail_ready(self, entity_id: int) -> None:
        for row in range(self.recent.count()):
            item = self.recent.item(row)
            if item.data(_ID) != entity_id:
                continue
            loaded = self.thumbnails.get(entity_id)
            if loaded is None:
                return  # requested; ready fires when it is
            if loaded.image is not None:
                item.setIcon(QIcon(QPixmap.fromImage(loaded.image)))
            else:
                item.setIcon(icon_for(loaded.icon))

    def _clicked(self, href: str) -> None:
        kind, _, value = href.partition(":")
        if kind == "type":
            self.open_type.emit(value)
        elif kind == "triage":
            self.open_triage.emit()
        elif kind == "root":
            self.configure_root.emit(value)
        elif kind == "tag":
            self.search_tag.emit(int(value))
        elif kind == "value":
            self.search_value.emit(*self._values[int(value)])


def _links(on_link: object) -> QLabel:
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    label.linkActivated.connect(on_link)
    return label


def _card(title: str, *widgets: QWidget) -> QFrame:
    card = QFrame()
    card.setObjectName(f"card_{title.lower().replace(' ', '_')}")
    card.setFrameShape(QFrame.Shape.StyledPanel)
    column = QVBoxLayout(card)
    heading = QLabel(f"<b>{html.escape(title)}</b>")
    column.addWidget(heading)
    for widget in widgets:
        column.addWidget(widget)
    column.addStretch(1)
    return card


def _tags(uses: tuple[TagUse, ...]) -> str:
    return "<br>".join(
        f'<a href="tag:{u.tag_id}">{html.escape(u.path)}</a>: {u.count:,}' for u in uses
    )


def _ago(when: datetime) -> str:
    seconds = (datetime.now(UTC) - when).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} hours ago"
    return f"on {when.astimezone():%Y-%m-%d}"
