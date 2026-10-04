"""The search results model: one row per entity, fetched lazily in pages (DESIGN.md §8, §12).

Setting a search counts the matches and loads the first page in a worker; the row count is
then the full total, so the scroll bar is right from the start. Other pages load in workers
when the view first asks for one of their rows; until then those rows show a placeholder.
Only the most recently used pages stay in memory.

Every search gets a new generation number, and results from an older generation are
dropped, so a slow query never overwrites a newer one.
"""

import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

import shiboken6
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    QThreadPool,
    Signal,
)
from sqlalchemy import ColumnElement, Connection

from tagalot.core.formats import format_value
from tagalot.core.keywords import keyword_index, keyword_key, keywords_of
from tagalot.core.search import SearchError, SearchHit, count_matches, run_search
from tagalot.core.search_fields import field_values, search_fields
from tagalot.core.search_spec import SearchSpec, SortKey
from tagalot.core.session import KeepSession
from tagalot.core.tags import PATH_SEPARATOR, TagTree, entity_tags, name_key
from tagalot.core.theme_schema import ThemeSchema
from tagalot.ui.workers import run_in_pool

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
MAX_PAGES = 50
"""Pages kept in memory; the least recently used beyond this are dropped and refetched."""

PLACEHOLDER = "…"
"""Shown in the first column of a row whose page hasn't loaded yet."""

Row = tuple[SearchHit, Mapping[str, Any]]
AnyIndex = QModelIndex | QPersistentModelIndex


@dataclass(frozen=True)
class ResultColumn:
    """A list column: ``key`` is ``"title"``, ``"type"``, or a theme field name."""

    key: str
    label: str
    sortable: bool = True
    numeric: bool = False
    display: str | None = None
    """The field's display format (``"bytes"``, ``"duration"``), if any."""


TAGS = "tags"
"""The key of the Tags column: an item's own tags, not a theme field."""
KEYWORDS = "keywords"
"""The key of the Keywords column: what an item's files say it is about (#295)."""


@dataclass(frozen=True)
class TagsValue:
    """The Tags column's cell: tag names for the list, full paths for the tooltip."""

    text: str
    tooltip: str


def tags_value(tree: TagTree, tag_ids: Iterable[int]) -> TagsValue:
    """Tag names (with the path when a name is ambiguous) in name order; deleted tags are
    skipped."""
    shown = sorted((t for t in tag_ids if t in tree), key=lambda t: name_key(tree.display_name(t)))
    return TagsValue(
        ", ".join(tree.display_name(t) for t in shown),
        "\n".join(PATH_SEPARATOR.join(tree.path(t)) for t in shown),
    )


def keywords_value(words: Sequence[str], index: dict[str, int], tree: TagTree) -> TagsValue:
    """An item's file keywords: the ones no tag matches first, then the rest; the
    tooltip says what each gives."""
    matched = {w: index.get(keyword_key(w)) for w in words}
    ordered = sorted(words, key=lambda w: (matched[w] is not None, w.casefold()))
    lines = []
    for word in ordered:
        tag_id = matched[word]
        if tag_id is not None and tag_id in tree:
            lines.append(f"{word} \u2192 {PATH_SEPARATOR.join(tree.path(tag_id))}")
        else:
            lines.append(f"{word}: no tag")
    return TagsValue(", ".join(ordered), "\n".join(lines))


def row_values(
    conn: Connection,
    schema: ThemeSchema,
    tree: TagTree,
    hits: Sequence[SearchHit],
    columns: Sequence[ResultColumn],
) -> dict[int, dict[str, Any]]:
    """The cell values for ``hits``: their theme fields and, if shown, their tags. Runs in a
    worker."""
    names = [c.key for c in columns if c.key not in ("title", "type", TAGS, KEYWORDS)]
    values = field_values(conn, schema, hits, names) if names else {}
    if any(c.key == TAGS for c in columns):
        tagged = entity_tags(conn, (h.id for h in hits))
        for hit in hits:
            values.setdefault(hit.id, {})[TAGS] = tags_value(tree, tagged.get(hit.id, ()))
    if any(c.key == KEYWORDS for c in columns):
        words = keywords_of(conn, (h.id for h in hits))
        index = keyword_index(tree)
        for hit in hits:
            cell = keywords_value(words.get(hit.id, []), index, tree)
            values.setdefault(hit.id, {})[KEYWORDS] = cell
    return values


def display_value(value: object, display: str | None = None) -> str:
    """How a field value reads in a list cell (in the field's display format, if any)."""
    formatted = format_value(value, display)
    if formatted is not None:
        return formatted
    match value:
        case None:
            return ""
        case TagsValue():
            return value.text
        case bool():
            return "Yes" if value else "No"
        case datetime():
            return value.astimezone().strftime("%Y-%m-%d %H:%M")
        case date():
            return value.isoformat()
        case float() if value.is_integer():
            return str(int(value))  # a book's number in its series: 2, not 2.0
        case _:
            return str(value)


def cell_tooltip(column: ResultColumn, row: Row, type_labels: Mapping[str, str]) -> str:
    """The tooltip of one cell: the full tag paths for Tags, else the cell's text."""
    value = row[1].get(column.key)
    return value.tooltip if isinstance(value, TagsValue) else cell_text(column, row, type_labels)


def cell_text(column: ResultColumn, row: Row, type_labels: Mapping[str, str]) -> str:
    """The text of one cell of a result row."""
    hit, values = row
    if column.key == "title":
        return hit.title
    if column.key == "type":
        return type_labels.get(hit.type, hit.type)
    return display_value(values.get(column.key), column.display)


class PreviewModel(QAbstractTableModel):
    """A few fixed result rows (a section of the grouped global search). Not sortable."""

    def __init__(
        self,
        columns: Sequence[ResultColumn],
        rows: Sequence[Row],
        type_labels: Mapping[str, str] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.columns = list(columns)
        self.rows = list(rows)
        self.type_labels = dict(type_labels or {})

    def hit(self, row: int) -> SearchHit | None:
        return self.rows[row][0] if 0 <= row < len(self.rows) else None

    def rowCount(self, parent: AnyIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: AnyIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.columns)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation != Qt.Orientation.Horizontal or not 0 <= section < len(self.columns):
            return None
        column = self.columns[section]
        if role == Qt.ItemDataRole.DisplayRole:
            return column.label
        if role == Qt.ItemDataRole.TextAlignmentRole and column.numeric:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def data(self, index: AnyIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        column = self.columns[index.column()]
        if role == Qt.ItemDataRole.TextAlignmentRole and column.numeric:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.DisplayRole:
            return cell_text(column, self.rows[index.row()], self.type_labels)
        if role == Qt.ItemDataRole.ToolTipRole:
            return cell_tooltip(column, self.rows[index.row()], self.type_labels)
        return None

    def flags(self, index: AnyIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemNeverHasChildren
        )


class ResultsModel(QAbstractTableModel):
    """Search results for the list layout. Call :meth:`set_search` to run a search.

    Signals: :attr:`counted` (the total, once a search has run), :attr:`failed` (a message
    for the user, for example an unknown field).
    """

    counted = Signal(int)
    failed = Signal(str)

    def __init__(
        self,
        session: KeepSession,
        *,
        type_labels: Mapping[str, str] | None = None,
        page_size: int = PAGE_SIZE,
        max_pages: int = MAX_PAGES,
        pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.type_labels = dict(type_labels or {})
        self.page_size = page_size
        self.max_pages = max_pages
        self._pool = pool
        self._columns: list[ResultColumn] = []
        self._extra_fields: list[str] = []
        self._spec: SearchSpec | None = None
        self._generation = 0
        self._total = 0
        self._pages: OrderedDict[int, list[Row]] = OrderedDict()
        self._loading: set[int] = set()
        self.searching = False
        """True from :meth:`set_search` until the count arrives (or the search fails)."""
        self._refreshing = False

    # --- running searches ---

    @property
    def spec(self) -> SearchSpec | None:
        return self._spec

    @property
    def columns(self) -> list[ResultColumn]:
        return list(self._columns)

    @property
    def total(self) -> int:
        return self._total

    @property
    def extra_fields(self) -> list[str]:
        return list(self._extra_fields)

    def set_extra_fields(self, names: Sequence[str]) -> None:
        """Also load these theme fields for every row (the grid's card lines), though no
        column shows them. Takes effect with the next search or refresh."""
        self._extra_fields = list(dict.fromkeys(names))

    def set_search(self, spec: SearchSpec, columns: Sequence[ResultColumn] | None = None) -> None:
        """Run ``spec``, clearing the current results, and show ``columns`` (default: keep
        the current ones)."""
        self.beginResetModel()
        if columns is not None:
            self._columns = list(columns)
        self._spec = spec
        self._total = 0
        self._pages.clear()
        self._loading.clear()
        self.endResetModel()
        self._start()

    def refresh(self) -> None:
        """Run the current search again, keeping the rows on screen until the new ones
        arrive (for example after a scan or tagging). If the number of results is the same,
        the rows are updated in place, so the view keeps its selection and scroll position."""
        if self._spec is not None:
            self._start(refreshing=True)

    def _start(self, *, refreshing: bool = False) -> None:
        assert self._spec is not None
        self._refreshing = refreshing
        self._generation += 1
        self._loading.clear()
        self.searching = True
        generation, spec = self._generation, self._spec
        fetch, reader = self._fetcher(spec), self.session.reader

        def job() -> tuple[int, list[Row]]:
            with reader.connect() as conn:
                return fetch(conn, count=True, offset=0)

        self._run(
            job,
            on_done=lambda result: self._counted(generation, *result),
            on_error=lambda error: self._search_failed(generation, error),
        )

    def _fetcher(self, spec: SearchSpec) -> Callable[..., tuple[int, list[Row]]]:
        """A function, safe to call in a worker, that loads one page (and maybe the count)."""
        session, limit = self.session, self.page_size
        shown = {c.key for c in self._columns}
        columns = list(self._columns) + [
            ResultColumn(name, name) for name in self._extra_fields if name not in shown
        ]

        def fetch(conn: Connection, *, count: bool, offset: int) -> tuple[int, list[Row]]:
            tree: TagTree = session.tag_cache.get()
            fields: dict[str, ColumnElement[Any]] = search_fields(session.schema, spec.types)
            total = count_matches(conn, spec, tree, fields) if count else -1
            hits = run_search(conn, spec, tree, offset=offset, limit=limit, fields=fields)
            values = row_values(conn, session.schema, tree, hits, columns)
            return total, [(h, values.get(h.id, {})) for h in hits]

        return fetch

    def _run[T](
        self,
        job: Callable[[], T],
        *,
        on_done: Callable[[T], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        """Run ``job`` in a worker; drop its result if this model was deleted meanwhile
        (its page closed)."""

        def done(result: T) -> None:
            if shiboken6.isValid(self):
                on_done(result)

        def error(e: BaseException) -> None:
            if shiboken6.isValid(self):
                on_error(e)

        run_in_pool(job, on_done=done, on_error=error, pool=self._pool)

    def _counted(self, generation: int, total: int, first: list[Row]) -> None:
        if generation != self._generation:
            return
        if self._refreshing and total == self._total:
            # Same rows, possibly new values (tags): update in place, keeping the selection.
            # Other pages reload when shown.
            self._pages.clear()
            self._pages[0] = first
            self._loading.clear()
            if total and self._columns:
                last = self.index(total - 1, len(self._columns) - 1)
                self.dataChanged.emit(self.index(0, 0), last)
            self.searching = False
            self.counted.emit(total)
            return
        self.beginResetModel()
        self._total = total
        self._pages.clear()
        self._pages[0] = first
        self._loading.clear()
        self.endResetModel()
        self.searching = False
        self.counted.emit(total)

    def _search_failed(self, generation: int, error: BaseException) -> None:
        if generation != self._generation:
            return
        self.searching = False
        if isinstance(error, SearchError):
            self.failed.emit(str(error))
        else:
            logger.error("Search failed", exc_info=error)
            self.failed.emit(f"The search failed: {error}")

    def _load_page(self, page: int) -> None:
        if page in self._loading or self._spec is None:
            return
        self._loading.add(page)
        generation, fetch = self._generation, self._fetcher(self._spec)
        reader, offset = self.session.reader, page * self.page_size

        def job() -> list[Row]:
            with reader.connect() as conn:
                return fetch(conn, count=False, offset=offset)[1]

        self._run(
            job,
            on_done=lambda rows: self._page_loaded(generation, page, rows),
            on_error=lambda error: self._search_failed(generation, error),
        )

    def _page_loaded(self, generation: int, page: int, rows: list[Row]) -> None:
        if generation != self._generation:
            return
        self._loading.discard(page)
        self._pages[page] = rows
        while len(self._pages) > self.max_pages:
            self._pages.popitem(last=False)
        first = page * self.page_size
        last = min(first + self.page_size, self._total) - 1
        if last >= first and self._columns:
            self.dataChanged.emit(self.index(first, 0), self.index(last, len(self._columns) - 1))

    # --- reading rows ---

    def row(self, row: int, *, load: bool = True) -> Row | None:
        """The hit and field values at ``row``, or ``None`` if its page isn't loaded (which
        starts loading it, unless ``load`` is false)."""
        if not 0 <= row < self._total:
            return None
        page, offset = divmod(row, self.page_size)
        rows = self._pages.get(page)
        if rows is None:
            if load:
                self._load_page(page)
            return None
        self._pages.move_to_end(page)
        return rows[offset] if offset < len(rows) else None  # fewer rows if the keep changed

    def hit(self, row: int) -> SearchHit | None:
        """The entity at ``row``, if its page is loaded."""
        found = self.row(row, load=False)
        return found[0] if found else None

    def entity_ids(self, rows: Iterable[int], on_done: Callable[[list[int]], None]) -> None:
        """Call ``on_done`` with the entity ids at ``rows`` (in row order). Rows on loaded
        pages are read directly; the rest come from one query, in a worker. If the search
        changes meanwhile, ``on_done`` is not called (the rows no longer mean the same)."""
        wanted = sorted({r for r in rows if 0 <= r < self._total})
        ids: dict[int, int] = {}
        for row in wanted:
            if (hit := self.hit(row)) is not None:
                ids[row] = hit.id
        missing = [r for r in wanted if r not in ids]
        if not missing or self._spec is None:
            on_done([ids[r] for r in wanted if r in ids])
            return
        generation, spec, session = self._generation, self._spec, self.session
        first, last = missing[0], missing[-1]

        def job() -> list[int]:
            tree = session.tag_cache.get()
            fields = search_fields(session.schema, spec.types)
            with session.reader.connect() as conn:
                hits = run_search(
                    conn, spec, tree, offset=first, limit=last - first + 1, fields=fields
                )
            return [h.id for h in hits]

        def done(found: list[int]) -> None:
            if generation != self._generation:
                return
            for row in missing:
                if row - first < len(found):
                    ids[row] = found[row - first]
            on_done([ids[r] for r in wanted if r in ids])

        self._run(job, on_done=done, on_error=lambda e: self._search_failed(generation, e))

    def loaded_pages(self) -> list[int]:
        return sorted(self._pages)

    # --- sorting ---

    def sort_column(self) -> tuple[int, Qt.SortOrder] | None:
        """The column and order of the search's first sort key, if it is a shown column."""
        if self._spec is None or not self._spec.sort:
            return None
        key = self._spec.sort[0]
        for i, column in enumerate(self._columns):
            if column.key == key.field:
                order = (
                    Qt.SortOrder.DescendingOrder if key.descending else Qt.SortOrder.AscendingOrder
                )
                return i, order
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Sort by a sortable column (a header click). Others are ignored."""
        if self._spec is None or not 0 <= column < len(self._columns):
            return
        info = self._columns[column]
        if not info.sortable:
            return
        key = SortKey(info.key, descending=order == Qt.SortOrder.DescendingOrder)
        if self._spec.sort[:1] == (key,):
            return
        self.set_search(replace(self._spec, sort=(key,)))

    # --- QAbstractTableModel ---

    def rowCount(self, parent: AnyIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else self._total

    def columnCount(self, parent: AnyIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._columns)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation != Qt.Orientation.Horizontal or not 0 <= section < len(self._columns):
            return None
        column = self._columns[section]
        if role == Qt.ItemDataRole.DisplayRole:
            return column.label
        if role == Qt.ItemDataRole.TextAlignmentRole and column.numeric:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def data(self, index: AnyIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.column() < len(self._columns):
            return None
        column = self._columns[index.column()]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column.numeric:
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            return None
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        found = self.row(index.row())
        if found is None:
            return (
                PLACEHOLDER if index.column() == 0 and role == Qt.ItemDataRole.DisplayRole else ""
            )
        if role == Qt.ItemDataRole.ToolTipRole:
            return cell_tooltip(column, found, self.type_labels)
        return cell_text(column, found, self.type_labels)

    def flags(self, index: AnyIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemNeverHasChildren
        )
