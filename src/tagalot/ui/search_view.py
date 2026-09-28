"""Search view: a heading, the result count, the filter bar, and the results (DESIGN.md §12).

Results use the list layout (columns); the grid and tree layouts come later. The global
search ("Search all") is **grouped**: one section per type with its first matches, until the
user picks "Show all" on a section (an "Only: <type>" chip) or only one type matches, when
it shows that type's full list. A search page is created once per navigation target and
re-runs its search after a scan.
"""

from collections.abc import Callable, Iterable
from dataclasses import replace

import shiboken6
from PySide6.QtCore import QItemSelectionModel, QThreadPool, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget

from tagalot.core.search import CORE_FIELDS, SearchError
from tagalot.core.search_fields import scope_fields, type_labels, type_plurals
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.tags import TagTree
from tagalot.ui.filter_bar import FilterBar, Filters
from tagalot.ui.grouped_results import GroupedResults, TypeGroup, load_groups
from tagalot.ui.models.results import ResultsModel
from tagalot.ui.result_table import (
    DEFAULT_HIDDEN,
    count_text,
    list_columns,
    make_result_table,
    set_column_widths,
)
from tagalot.ui.workers import run_in_pool

__all__ = ["SearchPage", "count_text", "list_columns"]


class SearchPage(QWidget):
    """One search: ``title``, a filter bar, and the entities ``spec`` finds.

    ``spec`` is the page's starting point (a theme view's types, toggles, and sort); the
    filter bar's tags and text are applied on top of it. With ``grouped`` (the global
    search), results are shown in sections by type.

    Emits :attr:`tags_dropped` with entity ids and tag ids when tags are dropped on results;
    the window does the tagging.
    """

    tags_dropped = Signal(list, list)
    hidden_columns_changed = Signal(list)
    """The column keys now hidden, after the user showed or hid one (to remember it)."""

    def __init__(
        self,
        session: KeepSession,
        title: str,
        spec: SearchSpec,
        *,
        grouped: bool = False,
        hidden_columns: Iterable[str] | None = None,
        pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.grouped = grouped
        self._base = spec
        self.hidden_columns = set(DEFAULT_HIDDEN if hidden_columns is None else hidden_columns)
        self._sort = spec.sort
        self._pool = pool
        self._generation = 0
        self._showing_sort = False  # true while the page itself moves the sort indicator
        self._plurals = type_plurals(session.schema)
        self.model = ResultsModel(session, type_labels=type_labels(session.schema), pool=pool)
        self.model.setParent(self)

        heading = QLabel(title)
        font = QFont(heading.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 2)
        heading.setFont(font)
        self.status = QLabel("Searching…")
        header_row = QHBoxLayout()
        header_row.addWidget(heading)
        header_row.addStretch(1)
        header_row.addWidget(self.status)

        self.filter_bar = FilterBar()
        self.filter_bar.changed.connect(self._filters_changed)

        self.table = make_result_table()
        self.table.setModel(self.model)
        header = self.table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        header.sortIndicatorChanged.connect(self._sort_clicked)

        self.table.tags_dropped.connect(self._tags_dropped_on_list)
        self.table.column_toggled.connect(self._column_toggled)

        self.groups = GroupedResults()
        self.groups.show_all.connect(self.show_all)
        self.groups.tags_dropped.connect(self.tags_dropped)
        self.groups.column_toggled.connect(self._column_toggled)
        self.groups.set_hidden_columns(self.hidden_columns)
        self.results = QStackedWidget()
        self.results.addWidget(self.table)
        self.results.addWidget(self.groups)

        layout = QVBoxLayout(self)
        layout.addLayout(header_row)
        layout.addWidget(self.filter_bar)
        layout.addWidget(self.results, 1)

        self.model.counted.connect(self._counted)
        self.model.failed.connect(self._failed)
        self.model.modelReset.connect(self._show_sort)
        # A reset (new results) keeps the selected items that are still listed.
        self._kept_selection: set[int] = set()
        self.model.modelAboutToBeReset.connect(self._remember_selection)
        self.model.modelReset.connect(self._restore_selection)
        self._run()
        self._load_tags()

    # --- running the search ---

    def current_spec(self, types: tuple[str, ...] | None = None) -> SearchSpec:
        """The page's spec with the filter bar applied, for ``types`` (default: the page's
        types, or the "Only" type). The last sort the user chose is kept when the listed
        types have its field; otherwise the page's default sort applies."""
        filters = self.filter_bar.filters()
        if types is None:
            types = (filters.only,) if filters.only is not None else self._base.types
        base = self._base
        text = " ".join(t for t in (base.text, filters.text) if t)
        # A sort chosen on one type's list (Year on albums) may not exist across types.
        valid = set(CORE_FIELDS) | {f.name for f in scope_fields(self.session.schema, types)}
        sort = self._sort if all(k.field in valid for k in self._sort) else base.sort
        return replace(
            base,
            types=types,
            include=base.include + tuple(t for t in filters.include if t not in base.include),
            exclude=base.exclude + tuple(t for t in filters.exclude if t not in base.exclude),
            text=text or None,
            sort=sort,
        )

    def showing_groups(self) -> bool:
        return self.results.currentWidget() is self.groups

    def refresh(self) -> None:
        """Run the search again (after a scan), keeping the results until new ones arrive,
        and reload the tag tree for the filter bar."""
        if self.grouped:
            self._run()
        else:
            self.model.refresh()
        self._load_tags()

    def show_all(self, type_id: str) -> None:
        """Narrow the global search to one type (a section's "Show all")."""
        self.filter_bar.set_only(type_id, self._plurals.get(type_id, type_id))

    def _run(self) -> None:
        self._generation += 1
        spec = self.current_spec()
        self.status.setText("Searching…")
        if not self.grouped or spec.types:
            self._show_list(spec)
            return
        generation, session = self._generation, self.session

        def done(groups: list[TypeGroup]) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._groups_loaded(spec, groups)

        def failed(error: BaseException) -> None:
            if shiboken6.isValid(self) and generation == self._generation:
                self._failed(
                    str(error) if isinstance(error, SearchError) else f"The search failed: {error}"
                )

        run_in_pool(
            lambda: load_groups(session, spec), on_done=done, on_error=failed, pool=self._pool
        )

    def _groups_loaded(self, spec: SearchSpec, groups: list[TypeGroup]) -> None:
        if len(groups) == 1:
            # One type matches (always, for a one-type theme): show its full list.
            self._show_list(self.current_spec((groups[0].type_id,)))
            return
        self.groups.set_groups(groups)
        self.results.setCurrentWidget(self.groups)
        total = sum(g.count for g in groups)
        self.status.setText(count_text(total) if total else "Nothing found")

    def _show_list(self, spec: SearchSpec) -> None:
        columns = list_columns(self.session.schema, spec.types)
        if columns != self.model.columns:
            self.model.set_search(spec, columns)
            set_column_widths(self.table, columns)
        elif spec != self.model.spec:
            self.model.set_search(spec)
        else:
            self.model.refresh()  # the same search again: keep the rows until new ones arrive
        self.table.set_columns(columns, self.hidden_columns)
        self.results.setCurrentWidget(self.table)

    def _load_tags(self) -> None:
        """Load the tag tree in a worker (the cache may need to read it from the keep)."""

        def loaded(tree: TagTree) -> None:
            if shiboken6.isValid(self.filter_bar):  # the page may have closed meanwhile
                self.filter_bar.set_tree(tree)

        run_in_pool(self.session.tag_cache.get, on_done=loaded, pool=self._pool)

    def _column_toggled(self, key: str, visible: bool) -> None:
        """Show or hide a column in the list and every section, and report it."""
        if visible:
            self.hidden_columns.discard(key)
        else:
            self.hidden_columns.add(key)
        self.table.set_columns(self.table.columns, self.hidden_columns)
        self.groups.set_hidden_columns(self.hidden_columns)
        self.hidden_columns_changed.emit(sorted(self.hidden_columns))

    def _remember_selection(self) -> None:
        self._kept_selection = {
            hit.id
            for index in self.table.selectionModel().selectedRows()
            if (hit := self.model.hit(index.row())) is not None
        }

    def _restore_selection(self) -> None:
        kept, self._kept_selection = self._kept_selection, set()
        if not kept:
            return
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for page in self.model.loaded_pages():
            first = page * self.model.page_size
            for row in range(first, min(first + self.model.page_size, self.model.total)):
                hit = self.model.hit(row)
                if hit is not None and hit.id in kept:
                    self.table.selectionModel().select(self.model.index(row, 0), flags)

    def selected_entity_ids(self, on_done: Callable[[list[int]], None]) -> None:
        """Call ``on_done`` with the entity ids selected in the list (or, when grouped, in
        every section). Selected rows that aren't loaded yet are looked up in a worker."""
        if self.showing_groups():
            ids = [
                hit.id
                for section in self.groups.sections
                for index in section.table.selectionModel().selectedRows()
                if (hit := section.model.hit(index.row())) is not None
            ]
            on_done(ids)
        else:
            rows = [index.row() for index in self.table.selectionModel().selectedRows()]
            self.model.entity_ids(rows, on_done)

    def _tags_dropped_on_list(self, rows: list[int], tag_ids: list[int]) -> None:
        # Rows far down a long selection may not be loaded yet: the model fetches their ids.
        self.model.entity_ids(
            rows, lambda ids: self.tags_dropped.emit(ids, tag_ids) if ids else None
        )

    def _filters_changed(self, _filters: Filters) -> None:
        self._run()

    # --- the list's count and sorting ---

    def _counted(self, total: int) -> None:
        self.status.setText(count_text(total) if total else "Nothing found")

    def _failed(self, message: str) -> None:
        self.status.setText(message)

    def _sort_clicked(self, column: int, _order: object) -> None:
        if self._showing_sort:
            return
        self.model.sort(column, self.table.horizontalHeader().sortIndicatorOrder())
        if self.model.spec is not None:
            self._sort = self.model.spec.sort  # kept when the filters change
        self._show_sort()  # a column that can't sort puts the indicator back

    def _show_sort(self) -> None:
        current = self.model.sort_column()
        header = self.table.horizontalHeader()
        self._showing_sort = True
        try:
            if current is None:
                header.setSortIndicator(-1, header.sortIndicatorOrder())
            else:
                header.setSortIndicator(*current)
        finally:
            self._showing_sort = False
        if self.model.searching:
            self.status.setText("Searching…")
