"""The tag tree as a Qt model, with a filter (DESIGN.md §12 "Tagging panel").

The model shows a :class:`~tagalot.core.tags.TagTree` snapshot. Filtering keeps the tags
whose name or an alias contains the text, plus their ancestors, so every match is shown in
place in the hierarchy. Matches are drawn in bold; ancestors shown only for context are not.
The whole tree is small (hundreds to low thousands of tags), so a new snapshot or filter
simply resets the model.

A tag's own icon that is a picture (#507) is drawn beside its name, after its color's
swatch; an animated GIF plays (a :class:`QMovie` per animated icon, few in a tree).

**Tag groups** (#535): with groups chosen (:meth:`TagTreeModel.set_groups`), each group is a
heading row, its name, with its tags under it (lifted to the top level, their sub-tags
under them), so a tag in two groups shown is listed twice. While filtering, matches outside
the groups shown follow under an **Other tags** heading, in place in the tree. Rows are
nodes of their own (an index's internal id is its node), each naming its tag, or none for a
heading.

**Only their tags** (#541, :meth:`TagTreeModel.set_applied_only`): only the tags the selected
items have (all or some of them), with their parents for context; with groups shown, those
outside the groups follow under **Other tags**, greyed.
"""

import base64
import html
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from PySide6.QtCore import (
    QAbstractItemModel,
    QBuffer,
    QByteArray,
    QIODevice,
    QMimeData,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QImage,
    QMovie,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import QApplication

from tagalot.core.tags import PATH_SEPARATOR, PictureData, TagTree, description_excerpt, search_key
from tagalot.ui.dnd import TAG_MIME, tags_mime
from tagalot.ui.icon_pictures import icon_image, is_animated, paint_icon_picture

TAG_ID_ROLE = Qt.ItemDataRole.UserRole + 1
"""The tag id of an index."""

EXCERPT_WIDTH = 32
"""Characters of a description shown next to a tag matched only through it."""


SWATCH_SLOT = 12
"""The width of a tag's color swatch beside an icon picture (#507)."""
ICON_SLOT = 16
"""A tag's icon picture in tag trees, at most this many pixels."""

TOOLTIP_PICTURE = 240
"""The longest side of a tag's picture in its tooltips (#506)."""


def picture_html(picture: PictureData, size: int = TOOLTIP_PICTURE) -> str:
    """A tag's picture as an ``<img>`` for rich-text tooltips, at most ``size`` pixels on
    its longer side (the data is in the tag, so nothing is read)."""
    scale = min(1.0, size / max(picture.width, picture.height, 1))
    width, height = round(picture.width * scale), round(picture.height * scale)
    kind = "png" if picture.data.startswith(b"\x89PNG") else "jpeg"
    data = base64.b64encode(picture.data).decode("ascii")
    return f'<img src="data:image/{kind};base64,{data}" width="{width}" height="{height}">'


def with_picture(tree: TagTree | None, tag_id: int, text: str) -> str:
    """``text`` (plain) under the tag's picture, as rich text, when it has one (#506);
    else ``text`` as it is."""
    picture = tree.picture(tag_id) if tree is not None and tag_id in tree else None
    if picture is None:
        return text
    lines = html.escape(text).replace("\n", "<br>")
    return f"<p>{picture_html(picture)}</p><p>{lines}</p>"


def tag_tooltip(tree: TagTree, tag_id: int) -> str:
    """A tag's tooltip: its picture (#506), its path, its description, and its aliases."""
    node = tree.node(tag_id)
    lines = [PATH_SEPARATOR.join(tree.path(tag_id))]
    if node.description:
        lines.append(node.description)
    if aliases := tree.aliases(tag_id):
        lines.append("Also: " + ", ".join(aliases))
    return with_picture(tree, tag_id, "\n".join(lines))


AnyIndex = QModelIndex | QPersistentModelIndex

OTHER_TAGS = "Other tags"
"""The heading over matches outside the groups shown, while filtering (#535)."""
OTHER_KEY = "other"


@dataclass(frozen=True)
class _Node:
    """One row: a tag (``tag_id``), or a heading (``tag_id`` ``None``) with its text."""

    tag_id: int | None
    parent: int
    """The parent row's node, 0 at the top level."""
    heading: str = ""
    key: str = ""
    """A heading's key: ``"group:<id>"``, or :data:`OTHER_KEY`."""
    dim: bool = False
    """Drawn greyed: a selected item's tag outside the groups shown (#541)."""


_ROOT = _Node(None, 0)


class TagTreeModel(QAbstractItemModel):
    """One column: tag names in tree order. Each index's internal id is its tag id.

    With items selected (:meth:`set_selection`), each tag has a check: checked if every
    selected item has it, partly checked if some do, unchecked if none do. Clicking a check
    doesn't change the model: it emits :attr:`check_clicked` with the tag id and whether to
    remove it (it was on all of them) or apply it to all; the summary is updated once the
    tagging is done.
    """

    check_clicked = Signal(int, bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.tree: TagTree | None = None
        self.filter_text = ""
        self.groups: tuple[int, ...] | None = None
        """The tag groups shown (#535), or ``None`` for every tag."""
        self.applied_only = False
        """Show only the tags the selected items have (#541)."""
        self._visible: frozenset[int] | None = None  # None: every tag
        self._matches: frozenset[int] = frozenset()
        self._alias_hits: dict[int, str] = {}  # matched through an alias, not the name
        self._description_hits: set[int] = set()  # matched only through the description
        self._nodes: list[_Node] = [_ROOT]
        self._children: dict[int, tuple[int, ...]] = {}
        """Each node's rows' nodes; 0, the top level."""
        self._row: dict[int, int] = {}
        """Each node's row under its parent."""
        self._tag_nodes: dict[int, list[int]] = {}
        """Each shown tag's rows, first in tree order first."""
        self._swatches: dict[str, QPixmap | None] = {}
        self._decorations: dict[int, QPixmap | None] = {}
        """Each tag's swatch and icon picture, drawn together, while the tree has icon
        pictures (#507)."""
        self._movies: dict[int, tuple[PictureData, QMovie]] = {}
        """The animated icon pictures playing, by tag."""
        self._frames: dict[int, QImage] = {}
        """Each animated icon's current frame."""
        self.selected_count = 0
        self._tag_counts: dict[int, int] = {}
        self._selected_types: frozenset[str] = frozenset()
        self._file_counts: dict[int, int] = {}
        """Of the selected items, how many have each tag only from their files (#295)."""
        """The selected items' types: tags none of them may have are hidden (#135)."""

    # --- content ---

    def set_tree(self, tree: TagTree) -> None:
        """Show ``tree``, keeping the current filter."""
        self.tree = tree
        self._rebuild()

    def set_filter(self, text: str) -> None:
        """Show only tags whose name or an alias contains ``text``, with their ancestors."""
        self.filter_text = text
        self._rebuild()

    def set_groups(self, groups: Iterable[int] | None) -> None:
        """Show only these tag groups, each as a heading over its tags (#535), or every tag
        (``None``). Groups the tree hasn't are left out; none left shows every tag."""
        self.groups = None if groups is None else tuple(groups)
        self._rebuild()

    def shown_groups(self) -> tuple[int, ...] | None:
        """The groups shown that the tree has, or ``None`` for every tag."""
        if self.groups is None or self.tree is None:
            return None
        known = {g.id for g in self.tree.groups()}
        shown = tuple(g for g in self.groups if g in known)
        return shown or None

    def set_applied_only(self, on: bool) -> None:
        """Show only the tags the selected items have (all or some of them), with their
        parents (#541), or every tag."""
        self.applied_only = on
        self._rebuild()

    @property
    def filtering(self) -> bool:
        return bool(search_key(self.filter_text))

    @property
    def narrowing(self) -> bool:
        """Only some tags show: filtering, or only the selected items' tags (#541)."""
        return self.filtering or self.applied_only

    def applied(self) -> frozenset[int]:
        """The tags the selected items have, all or some of them."""
        tree = self.tree
        return frozenset(t for t, n in self._tag_counts.items() if n and tree and t in tree)

    def matches(self) -> frozenset[int]:
        """The tags the filter matched (empty when not filtering)."""
        return self._matches

    def visible_count(self) -> int:
        """How many tag rows are shown (a tag listed twice counts twice; headings don't)."""
        return sum(len(nodes) for nodes in self._tag_nodes.values())

    def index_of(self, tag_id: int) -> QModelIndex:
        """The index of a shown tag (its first row), or an invalid index."""
        nodes = self._tag_nodes.get(tag_id)
        return self._index(nodes[0]) if nodes else QModelIndex()

    def indexes_of(self, tag_id: int) -> list[QModelIndex]:
        """Every row of a shown tag (it can be under two group headings)."""
        return [self._index(n) for n in self._tag_nodes.get(tag_id, ())]

    def _index(self, node: int) -> QModelIndex:
        return self.createIndex(self._row[node], 0, node)

    def _node(self, index: AnyIndex) -> int:
        return int(index.internalId()) if index.isValid() else 0

    def tag_id(self, index: AnyIndex) -> int | None:
        """The tag of a row; ``None`` for a heading or an invalid index."""
        node = self._node(index)
        return self._nodes[node].tag_id if 0 < node < len(self._nodes) else None

    def heading_key(self, index: AnyIndex) -> str | None:
        """A heading's key (``"group:<id>"`` or ``"other"``); ``None`` for a tag (#535)."""
        node = self._node(index)
        if 0 < node < len(self._nodes) and self._nodes[node].tag_id is None:
            return self._nodes[node].key
        return None

    def headings(self) -> list[QModelIndex]:
        """The heading rows, in order."""
        return [self._index(n) for n in self._children.get(0, ()) if self._nodes[n].key]

    def first_match(self) -> QModelIndex:
        """The first matching tag in tree order (for the keyboard), or an invalid index."""
        for node in self._walk(0):
            if self._nodes[node].tag_id in self._matches:
                return self._index(node)
        return QModelIndex()

    def _walk(self, parent: int) -> list[int]:
        order = []
        for child in self._children.get(parent, ()):
            order.append(child)
            order.extend(self._walk(child))
        return order

    def decoration_size(self) -> QSize | None:
        """The size of the tags' decorations while the tree has icon pictures (a swatch and
        an icon side by side, #507); ``None`` for the view's own."""
        if self.tree is None or not self.tree.icon_pictures():
            return None
        return QSize(SWATCH_SLOT + ICON_SLOT, ICON_SLOT)

    def _rebuild(self) -> None:
        self.beginResetModel()
        tree = self.tree
        self._nodes, self._children, self._row, self._tag_nodes = [_ROOT], {}, {}, {}
        self._decorations = {}
        self._play(tree)
        if tree is None:
            self._visible, self._matches = None, frozenset()
        else:
            if self.filtering:
                self._matches = tree.matching(self.filter_text)
                self._visible = tree.with_ancestors(self._matches)
                needle = search_key(self.filter_text)
                self._alias_hits, self._description_hits = {}, set()
                for tag_id in self._matches:
                    if needle in search_key(tree.node(tag_id).name):
                        continue
                    aliases = [a for a in tree.aliases(tag_id) if needle in search_key(a)]
                    if aliases:
                        self._alias_hits[tag_id] = aliases[0]
                    else:
                        self._description_hits.add(tag_id)
            else:
                self._visible, self._matches = None, frozenset()
                self._alias_hits, self._description_hits = {}, set()
            if self.applied_only:
                self._visible = tree.with_ancestors(self._targets())
            hidden = self._out_of_scope(tree)
            if hidden:
                shown = set(self._visible if self._visible is not None else tree) - hidden
                self._visible = tree.with_ancestors(shown)
            groups = self.shown_groups()
            if groups is None:
                self._place(0, self._subtrees(tree, tree.children(None), 0, self._visible))
            else:
                self._grouped(tree, groups)
        self.endResetModel()

    def _targets(self) -> frozenset[int]:
        """The tags narrowing picks (shown with their parents): the matches, the selected
        items' tags (#541), or both."""
        if self.applied_only:
            applied = self.applied()
            return applied & self._matches if self.filtering else applied
        return self._matches

    def _grouped(self, tree: TagTree, groups: tuple[int, ...]) -> None:
        """Each group shown as a heading over its tags; while narrowing (filtering, or only
        the selected items' tags), the rest under **Other tags** (#535, #541): greyed when
        they are the selected items' tags."""
        visible = self._visible
        headings: list[int] = []
        inside: set[int] = set()
        for group in tree.groups():
            if group.id not in groups:
                continue
            inside |= tree.expand(group.tags)
            tops = [t for t in tree.group_tops(group.id) if visible is None or t in visible]
            if not tops and self.narrowing:
                continue  # nothing in it matches
            heading = self._add(_Node(None, 0, group.name, f"group:{group.id}"))
            self._place(heading, self._subtrees(tree, tops, heading, visible))
            headings.append(heading)
        if self.narrowing:
            other = {m for m in self._targets() if m not in inside}
            if visible is not None:
                other &= visible
            if other:
                around = tree.with_ancestors(other)
                heading = self._add(_Node(None, 0, OTHER_TAGS, OTHER_KEY))
                rows = self._subtrees(
                    tree, tree.children(None), heading, around, dim=self.applied_only
                )
                self._place(heading, rows)
                headings.append(heading)
        self._place(0, headings)

    def _add(self, node: _Node) -> int:
        self._nodes.append(node)
        number = len(self._nodes) - 1
        if node.tag_id is not None:
            self._tag_nodes.setdefault(node.tag_id, []).append(number)
        return number

    def _subtrees(
        self,
        tree: TagTree,
        tags: Iterable[int],
        parent: int,
        shown: frozenset[int] | None,
        *,
        dim: bool = False,
    ) -> list[int]:
        """Rows for ``tags`` under ``parent``, with their sub-tags in ``shown`` (``None``:
        every one), greyed with ``dim``."""
        rows = []
        for tag_id in tags:
            if shown is not None and tag_id not in shown:
                continue
            node = self._add(_Node(tag_id, parent, dim=dim))
            self._place(node, self._subtrees(tree, tree.children(tag_id), node, shown, dim=dim))
            rows.append(node)
        return rows

    def is_dim(self, index: AnyIndex) -> bool:
        """The row is greyed: a selected item's tag outside the groups shown (#541)."""
        node = self._node(index)
        return 0 < node < len(self._nodes) and self._nodes[node].dim

    def _place(self, parent: int, rows: list[int]) -> None:
        self._children[parent] = tuple(rows)
        for row, node in enumerate(rows):
            self._row[node] = row

    # --- QAbstractItemModel ---

    def index(self, row: int, column: int, parent: AnyIndex = QModelIndex()) -> QModelIndex:  # noqa: B008
        kids = self._children.get(self._node(parent), ())
        if not 0 <= column < self.columnCount() or not 0 <= row < len(kids):
            return QModelIndex()
        return self.createIndex(row, column, kids[row])

    def parent(self, index: AnyIndex = QModelIndex()) -> QModelIndex:  # type: ignore[override]  # noqa: B008
        node = self._node(index)
        if not 0 < node < len(self._nodes):
            return QModelIndex()
        parent = self._nodes[node].parent
        return QModelIndex() if parent == 0 else self._index(parent)

    def rowCount(self, parent: AnyIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.column() > 0:
            return 0
        return len(self._children.get(self._node(parent), ()))

    def columnCount(self, parent: AnyIndex = QModelIndex()) -> int:  # noqa: B008
        return 1

    def hasChildren(self, parent: AnyIndex = QModelIndex()) -> bool:  # noqa: B008
        return self.rowCount(parent) > 0

    def data(self, index: AnyIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        tag_id = self.tag_id(index)
        tree = self.tree
        if tree is None:
            return None
        if tag_id is None:
            return self._heading_data(index, role)
        node = tree.node(tag_id)
        if role == Qt.ItemDataRole.DisplayRole:
            name = f"{node.icon} {node.name}" if node.icon else node.name  # its own (#398)
            alias = self._alias_hits.get(tag_id)
            if alias is not None:
                return f"{name} ({alias})"
            if tag_id in self._description_hits and node.description:
                excerpt = description_excerpt(node.description, self.filter_text, EXCERPT_WIDTH)
                return f"{name} — {excerpt}"
            return name
        if role == Qt.ItemDataRole.EditRole:
            return node.name  # renaming edits the name alone, not its icon
        if role == TAG_ID_ROLE:
            return tag_id
        if role == Qt.ItemDataRole.CheckStateRole:
            return self.check_state(tag_id)
        if role == Qt.ItemDataRole.ToolTipRole:
            tooltip = tag_tooltip(tree, tag_id)
            if self.selected_count:
                count = self._tag_counts.get(tag_id, 0)
                items = "item" if self.selected_count == 1 else "items"
                tooltip += f"\n\nOn {count} of the {self.selected_count} selected {items}"
                from_files = self._file_counts.get(tag_id, 0)
                if from_files:
                    tooltip += f"\nFrom the files of {from_files} (their keywords)"
            return tooltip
        if role == Qt.ItemDataRole.ForegroundRole and self.is_dim(index):
            return QApplication.palette().color(QPalette.ColorRole.PlaceholderText)
        if role == Qt.ItemDataRole.FontRole:
            bold = tag_id in self._matches
            italic = self.from_files_only(tag_id)
            if bold or italic:
                font = QFont()
                font.setBold(bold)
                font.setItalic(italic)
                return font
        if role == Qt.ItemDataRole.DecorationRole and tree.icon_pictures():
            if tag_id not in self._decorations:
                self._decorations[tag_id] = self._decoration(tree, tag_id)
            pixmap = self._decorations[tag_id]
            return QIcon(pixmap) if pixmap is not None else None  # a new one each time
        if role == Qt.ItemDataRole.DecorationRole and node.color:
            return self._swatch(node.color)
        return None

    def _heading_data(self, index: AnyIndex, role: int) -> Any:
        """A heading's text, drawn small and dim in capitals, like a section's (#535)."""
        node = self._node(index)
        if not 0 < node < len(self._nodes):
            return None
        heading = self._nodes[node]
        if role == Qt.ItemDataRole.DisplayRole:
            return heading.heading
        if role == Qt.ItemDataRole.FontRole:
            font = QFont()
            font.setCapitalization(QFont.Capitalization.AllUppercase)
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
            return font
        if role == Qt.ItemDataRole.ForegroundRole:
            return QApplication.palette().color(QPalette.ColorRole.PlaceholderText)
        if role == Qt.ItemDataRole.ToolTipRole:
            if heading.key == OTHER_KEY:
                return "Tags that match, outside the groups shown"
            return f"The tag group “{heading.heading}” (Groups ▾ chooses which show)"
        return None

    def _decoration(self, tree: TagTree, tag_id: int) -> QPixmap | None:
        """The tag's swatch (if it has a color) and its own icon picture (if it has one),
        side by side, drawn at twice the size for high-density screens."""
        node = tree.node(tag_id)
        picture = tree.icon_picture(tag_id)
        color = QColor(node.color) if node.color else None
        if picture is None and (color is None or not color.isValid()):
            return None
        scale = 2
        pixmap = QPixmap((SWATCH_SLOT + ICON_SLOT) * scale, ICON_SLOT * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            if color is not None and color.isValid():
                side = 10 * scale
                top = (ICON_SLOT * scale - side) // 2
                painter.fillRect(QRect(0, top, side, side), color)
            if picture is not None:
                frame = self._frames.get(tag_id) or icon_image(picture)
                box = QRect(SWATCH_SLOT * scale, 0, ICON_SLOT * scale, ICON_SLOT * scale)
                paint_icon_picture(painter, box, frame)
        finally:
            painter.end()
        pixmap.setDevicePixelRatio(scale)
        return pixmap

    def _play(self, tree: TagTree | None) -> None:
        """Play the tree's animated icon pictures; stop those no longer there."""
        wanted = {t: p for t, p in (tree.icon_pictures() if tree else {}).items() if is_animated(p)}
        for tag_id, (picture, movie) in list(self._movies.items()):
            if wanted.get(tag_id) != picture:
                movie.stop()
                movie.deleteLater()
                del self._movies[tag_id]
                self._frames.pop(tag_id, None)
        for tag_id, picture in wanted.items():
            if tag_id in self._movies:
                continue
            movie = QMovie(self)
            buffer = QBuffer(movie)
            buffer.setData(QByteArray(picture.data))
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            movie.setDevice(buffer)
            movie.setCacheMode(QMovie.CacheMode.CacheAll)
            movie.frameChanged.connect(partial(self._next_frame, tag_id))
            self._movies[tag_id] = (picture, movie)
            movie.start()

    def _next_frame(self, tag_id: int, _frame: int) -> None:
        playing = self._movies.get(tag_id)
        if playing is None:
            return
        self._frames[tag_id] = playing[1].currentImage()
        self._decorations.pop(tag_id, None)
        for index in self.indexes_of(tag_id):
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])

    def flags(self, index: AnyIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        if self.tag_id(index) is None:
            return Qt.ItemFlag.ItemIsEnabled  # a heading: not selected, dragged, or ticked
        flags = (
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        )
        if self.selected_count:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    # --- the selection summary (DESIGN.md §12) ---

    def set_selection(
        self,
        selected_count: int,
        tag_counts: dict[int, int],
        types: frozenset[str] = frozenset(),
        file_counts: dict[int, int] | None = None,
    ) -> bool:
        """How many items are selected, how many of them carry each tag directly, and their
        types. Returns whether the tags shown changed (the model was reset): tags limited to
        other types are hidden, unless a selected item has one (#135)."""
        before = self._out_of_scope(self.tree) if self.tree is not None else frozenset()
        self.selected_count = selected_count
        self._tag_counts = dict(tag_counts) if selected_count else {}
        self._selected_types = frozenset(types) if selected_count else frozenset()
        self._file_counts = dict(file_counts or {}) if selected_count else {}
        after = self._out_of_scope(self.tree) if self.tree is not None else frozenset()
        if after != before or self.applied_only:  # only their tags: which show changes (#541)
            self._rebuild()
            return True
        self._all_rows_changed()
        return False

    def _out_of_scope(self, tree: TagTree) -> frozenset[int]:
        """Tags that none of the selected items may have and none of them has."""
        if not self._selected_types:
            return frozenset()
        hidden = set()
        for tag_id in tree:
            allowed = tree.scope(tag_id)
            if (
                allowed is not None
                and not allowed & self._selected_types
                and not self._tag_counts.get(tag_id)
            ):
                hidden.add(tag_id)
        return frozenset(hidden)

    def from_files_only(self, tag_id: int) -> bool:
        """Every selected item that has the tag has it only from its files' keywords."""
        count = self._tag_counts.get(tag_id, 0)
        return bool(count) and self._file_counts.get(tag_id, 0) == count

    def check_state(self, tag_id: int) -> Qt.CheckState | None:
        """All, some, or none of the selected items have ``tag_id``; ``None`` if nothing is
        selected."""
        if not self.selected_count:
            return None
        count = self._tag_counts.get(tag_id, 0)
        if count >= self.selected_count:
            return Qt.CheckState.Checked
        return Qt.CheckState.PartiallyChecked if count else Qt.CheckState.Unchecked

    def setData(self, index: AnyIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        tag_id = self.tag_id(index)
        if role != Qt.ItemDataRole.CheckStateRole or tag_id is None or not self.selected_count:
            return False
        # On all of them: remove it from all. On some or none: apply it to all.
        self.check_clicked.emit(tag_id, self.check_state(tag_id) == Qt.CheckState.Checked)
        return False  # the summary changes when the tagging is done

    def _all_rows_changed(self) -> None:
        for nodes in self._tag_nodes.values():
            for node in nodes:
                index = self._index(node)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])

    # --- dragging tags onto items (DESIGN.md §12) ---

    def mimeTypes(self) -> list[str]:
        return [TAG_MIME]

    def mimeData(self, indexes: Sequence[QModelIndex]) -> QMimeData:
        ids = [t for i in indexes if (t := self.tag_id(i)) is not None]
        return tags_mime(ids)

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction

    def _swatch(self, color: str) -> QIcon | None:
        """A small square of the tag's color; ``None`` for a color Qt can't parse.

        The pixmap is cached, but each call returns a new ``QIcon``: returning one cached
        icon object from ``data()`` twice crashes PySide6 (6.11), which takes ownership of
        what ``data()`` returns.
        """
        if color not in self._swatches:
            qcolor = QColor(color)
            pixmap = None
            if qcolor.isValid():
                pixmap = QPixmap(10, 10)
                pixmap.fill(qcolor)
            self._swatches[color] = pixmap
        pixmap = self._swatches[color]
        return QIcon(pixmap) if pixmap is not None else None
