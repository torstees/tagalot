"""Search view: a heading, the result count, the filter bar, and the results (DESIGN.md §12).

Results show as a list (columns) or a grid (thumbnail cards), switched with the two
buttons by the count; the tree layout comes later. The global search ("Search all") is
**grouped**: one section per type with its first matches, until the
user picks "Show all" on a section (an "Only: <type>" chip) or only one type matches, when
it shows that type's full list. A search page is created once per navigation target and
re-runs its search after a scan.
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace

import shiboken6
from PySide6.QtCore import (
    QItemSelectionModel,
    QModelIndex,
    QPoint,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tagalot.core.actions import actions_for
from tagalot.core.containers import containers_for, hand_made_types
from tagalot.core.saved_searches import SavedDefinition
from tagalot.core.search import CORE_FIELDS, SearchError, SearchHit, choice_counts
from tagalot.core.search_fields import (
    contained_types,
    scope_fields,
    scoped_tables,
    search_fields,
    type_labels,
    type_plurals,
)
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.tags import TagTree
from tagalot.themes.api import entity_label
from tagalot.ui.field_filters import ChoiceCounts, FilterField
from tagalot.ui.file_actions import (
    FileOpener,
    add_file_actions,
    add_open_keys,
    file_kind,
    opens_file,
)
from tagalot.ui.filter_bar import FilterBar, Filters
from tagalot.ui.grouped_results import GroupedResults, TypeGroup, load_groups
from tagalot.ui.lookups import LOOK_UP_TIP
from tagalot.ui.models.results import ResultColumn, ResultsModel
from tagalot.ui.preview import PreviewStrip
from tagalot.ui.result_grid import CardLines, ResultGrid, grid_icon
from tagalot.ui.result_table import (
    DEFAULT_HIDDEN,
    count_text,
    hidden_keys,
    list_columns,
    make_result_table,
    set_column_widths,
)
from tagalot.ui.result_tree import ResultTree, ResultTreeModel
from tagalot.ui.thumbnails import ThumbnailLoader
from tagalot.ui.workers import run_in_pool

REREAD_TIP = (
    "Read the item's details from its file again (for example after changing the file); "
    "what you edited stays"
)

logger = logging.getLogger(__name__)

LAYOUTS = ("list", "grid", "tree")

PREVIEW_DELAY_MS = 120
"""How long the selection must settle before the preview strip updates."""

__all__ = ["SearchPage", "count_text", "list_columns"]


WRITE_BACK_TIP = (
    "Write its tags, and the fields you edited, into the front matter of its Markdown files "
    "(in folders that allow it), after showing you the change"
)


def searches_contents(session: KeepSession) -> bool:
    """The keep searches inside documents (§8): the filter bar offers Contents."""
    return session.keep.config.contents_index is not None and session.has_documents


def writes_back(session: KeepSession, type_id: str) -> bool:
    """Whether items of ``type_id`` offer Write to file… (the theme's ``write_back``)."""
    theme = session.schema.theme
    return any(theme.type_id_of(t) == type_id for t in theme.write_back)


def add_reread_actions(menu: QMenu, reread: Callable[[bool], None]) -> None:
    """Add "Re-read from file" and its replacing variant to ``menu``; ``reread(replace)``."""
    keep = menu.addAction("Re-read from file")
    keep.setToolTip(REREAD_TIP)
    keep.triggered.connect(lambda: reread(False))
    replace = menu.addAction("Re-read from file, replacing my edits\u2026")
    replace.setToolTip("Read the details from the file again, and use them instead of your edits")
    replace.triggered.connect(lambda: reread(True))


class SearchPage(QWidget):
    """One search: ``title``, a filter bar, and the entities ``spec`` finds.

    ``spec`` is the page's starting point (a theme view's types, toggles, and sort); the
    filter bar's tags and text are applied on top of it. With ``grouped`` (the global
    search), results are shown in sections by type.

    Emits :attr:`tags_dropped` with entity ids and tag ids when tags are dropped on results;
    the window does the tagging.
    """

    save_requested = Signal()
    """Save… was clicked (#127): the window saves the page."""
    tags_dropped = Signal(list, list)
    hidden_columns_changed = Signal(list)
    """The column keys now hidden, after the user showed or hid one (to remember it)."""
    shown_columns_changed = Signal(list)
    """The column keys the user showed (of those hidden by default), to remember it."""
    selection_changed = Signal()
    """The selected items may be different (a click, new results, grouped or not)."""
    layout_changed = Signal(str)
    """The user switched to ``"list"``, ``"grid"``, or ``"tree"`` (to remember it)."""
    toggles_changed = Signal(dict)
    """The user changed "Contained", "Inherit tags", or "By contents":
    ``{"show_contained": …, "inherit_tags": …, "aggregate_up": …}`` (to remember it)."""
    card_lines_changed = Signal(object)
    """The user chose card lines (a list of field names), or ``None`` for the theme's."""
    zoom_requested = Signal(int)
    """Ctrl+wheel over the grid: +1 for bigger thumbnails, -1 for smaller."""
    open_requested = Signal(int)
    """An item was double-clicked (or Enter pressed on it): its entity id."""
    reread_requested = Signal(list, bool)
    new_item_requested = Signal(str)
    """New project… (#329): make an item of this hand-made type."""
    add_to_requested = Signal(str, list)
    """Add to project… (#329): (the container type, the items to put in one)."""
    write_back_requested = Signal(list)
    look_up_requested = Signal(list)
    """Look up online (#340): the selected items' ids."""
    """Write to file… on these items (#299)."""
    action_requested = Signal(str, list)
    """Run a theme action: (method name, entity ids)."""
    """Read these entities' files again; true: replacing what the user edited."""

    def __init__(
        self,
        session: KeepSession,
        title: str,
        spec: SearchSpec,
        *,
        grouped: bool = False,
        hidden_columns: Iterable[str] | None = None,
        shown_columns: Iterable[str] = (),
        layout_mode: str = "list",
        card_lines: Sequence[str] | None = None,
        thumbnails: ThumbnailLoader | None = None,
        thumbnail_size: int = 128,
        preview: bool = True,
        toggles: dict[str, bool] | None = None,
        pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._menu_actions: list[tuple[str, Callable[[list[int]], None]]] = []
        self.session = session
        self.grouped = grouped
        self.title = title
        self._base = spec
        self.hidden_columns = set(DEFAULT_HIDDEN if hidden_columns is None else hidden_columns)
        self.shown_columns = set(shown_columns)
        """Columns hidden by default (fields not on cards) that the user showed (#314)."""
        self._sort = spec.sort
        self._pool = pool
        self._generation = 0
        self._showing_sort = False  # true while the page itself moves the sort indicator
        self._plurals = type_plurals(session.schema)
        self.layout_mode = layout_mode if layout_mode in LAYOUTS else "list"
        self._card_override = list(card_lines) if card_lines is not None else None
        self.size_menu: QMenu | None = None
        self.file_opener: FileOpener | None = None
        """Opens items' files (the window sets it); without one, menus have no file actions."""
        """The window's Thumbnail size menu, offered in the grid's context menu too."""
        self.model = ResultsModel(session, type_labels=type_labels(session.schema), pool=pool)
        self.model.setParent(self)

        heading = QLabel(title)
        font = QFont(heading.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 2)
        heading.setFont(font)
        self.status = QLabel("Searching…")
        header_row = QHBoxLayout()
        self.header_row = header_row
        """The heading's row: a page holding this search can add buttons to it."""
        header_row.addWidget(heading)
        for type_id in hand_made_types(session.schema):  # New project… (#329)
            if type_id in spec.types:
                noun = entity_label(session.schema.by_type_id(type_id).entity).lower()
                new = QPushButton(f"New {noun}\u2026")
                new.setObjectName(f"new_{type_id}")
                new.setFlat(True)
                new.setCursor(Qt.CursorShape.PointingHandCursor)
                new.setToolTip(f"Make a {noun} to put items in")
                new.clicked.connect(lambda _=False, t=type_id: self.new_item_requested.emit(t))
                header_row.addWidget(new)
        header_row.addStretch(1)
        self.save_button = QPushButton("Save\u2026")
        self.save_button.setObjectName("save_search")
        self.save_button.setFlat(True)
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.setToolTip("Keep this search, with its chips and layout, under SAVED")
        self.save_button.clicked.connect(self.save_requested)
        header_row.addWidget(self.save_button)
        header_row.addWidget(self.status)
        self.list_button = self._layout_button("list", "Show the results as a list")
        self.grid_button = self._layout_button("grid", "Show the results as thumbnails")
        self.tree_button = self._layout_button(
            "tree", "Show the results as a tree: expand an item to see what it contains"
        )
        self._layout_buttons = QButtonGroup(self)
        self._layout_buttons.setExclusive(True)
        for button in (self.list_button, self.grid_button, self.tree_button):
            self._layout_buttons.addButton(button)
            header_row.addWidget(button)
        self.list_button.clicked.connect(lambda: self._layout_clicked("list"))
        self.grid_button.clicked.connect(lambda: self._layout_clicked("grid"))
        self.tree_button.clicked.connect(lambda: self._layout_clicked("tree"))

        self.filter_bar = FilterBar()
        self.filter_bar.choice_loader = self._load_choices
        # The view's toggles, unless the user chose otherwise before.
        chosen = toggles or {}
        self._toggles = {
            "show_contained": bool(chosen.get("show_contained", spec.show_contained)),
            "inherit_tags": bool(chosen.get("inherit_tags", spec.inherit_tags)),
            "aggregate_up": bool(chosen.get("aggregate_up", spec.aggregate_up)),
            "contents": bool(chosen.get("contents", spec.contents)),
        }
        self.filter_bar.set_toggles(**self._toggles)
        self.filter_bar.set_contents_available(searches_contents(session))
        self.filter_bar.changed.connect(self._filters_changed)

        self.table = make_result_table()
        self.table.setModel(self.model)
        header = self.table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        header.sortIndicatorChanged.connect(self._sort_clicked)

        self.table.tags_dropped.connect(self._tags_dropped_on_list)
        self.table.column_toggled.connect(self._column_toggled)
        self.table.selectionModel().selectionChanged.connect(self.selection_changed)
        self.model.modelReset.connect(self.selection_changed)

        self.grid = ResultGrid(thumbnails, thumbnail_size)
        self.grid.setModel(self.model)
        # One selection for both layouts: switching keeps what is selected.
        self.grid.setSelectionModel(self.table.selectionModel())
        self.grid.tags_dropped.connect(self._tags_dropped_on_list)
        self.grid.zoom_requested.connect(self.zoom_requested)
        self.grid.menu_requested.connect(self._grid_menu)
        self.table.activated.connect(self._activated)
        self.grid.activated.connect(self._activated)
        add_open_keys(self.table, self._activated)
        add_open_keys(self.grid, self._activated)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)

        self.tree_model = ResultTreeModel(self.model, session, pool)
        self.tree_model.setParent(self)
        self.tree = ResultTree()
        self.tree.setModel(self.tree_model)
        tree_header = self.tree.header()
        tree_header.setSortIndicatorShown(True)
        tree_header.setSectionsClickable(True)
        tree_header.sortIndicatorChanged.connect(self._sort_clicked)
        self.tree.selectionModel().selectionChanged.connect(self.selection_changed)
        self.tree.activated.connect(self._tree_activated)
        add_open_keys(self.tree, self._tree_activated)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.tags_dropped.connect(self.tags_dropped)

        self.groups = GroupedResults()
        self.groups.show_all.connect(self.show_all)
        self.groups.tags_dropped.connect(self.tags_dropped)
        self.groups.column_toggled.connect(self._column_toggled)
        self.groups.set_hidden_columns(self.hidden_columns, self.shown_columns)
        self.groups.selection_changed.connect(self.selection_changed)
        self.groups.hit_activated.connect(self.activate)
        self.groups.item_menu_requested.connect(lambda hit, at: self.item_menu(hit).exec(at))
        self.results = QStackedWidget()
        self.results.addWidget(self.table)
        self.results.addWidget(self.grid)
        self.results.addWidget(self.tree)
        self.results.addWidget(self.groups)
        self._show_layout_buttons()

        layout = QVBoxLayout(self)
        layout.addLayout(header_row)
        layout.addWidget(self.filter_bar)
        layout.addWidget(self.results, 1)
        self.preview = PreviewStrip(session, thumbnails, pool)
        self.preview.open_requested.connect(self.open_requested)
        self.preview.setVisible(preview)
        layout.addWidget(self.preview)
        # Selections change in bursts (Shift+arrows): preview once they settle.
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(PREVIEW_DELAY_MS)
        self._preview_timer.timeout.connect(self._update_preview)
        self.selection_changed.connect(self._preview_timer.start)

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

    # --- saved searches (#127) ---

    def saved_definition(self) -> SavedDefinition:
        """The page as it stands, to save: its own search with the sort chosen, the filter
        bar's chips and toggles, and how it's shown."""
        f = self.filter_bar.filters()
        filters = SearchSpec(
            types=(f.only,) if f.only is not None else (),
            include=f.include,
            exclude=f.exclude,
            text=f.text or None,
            within=f.within,
            fields=f.fields,
            inherit_tags=f.inherit_tags,
            show_contained=f.show_contained,
            aggregate_up=f.aggregate_up,
            contents=f.contents,
        )
        return SavedDefinition(
            base=replace(self._base, sort=self._sort),
            filters=filters,
            within_title=self.filter_bar.within_title,
            within_type=f.within_type or "",
            layout=self.layout_mode,
            grouped=self.grouped,
        )

    def restore(self, saved: SavedDefinition) -> None:
        """Put a saved search's chips and toggles back on the filter bar."""
        f, bar = saved.filters, self.filter_bar
        bar.set_toggles(
            show_contained=f.show_contained,
            inherit_tags=f.inherit_tags,
            aggregate_up=f.aggregate_up,
            contents=f.contents,
        )
        if f.within is not None:
            bar.set_within(f.within, saved.within_title, saved.within_type)
        if f.types:
            bar.set_only(f.types[0], self._plurals.get(f.types[0], f.types[0]))
        self.filter_bar.set_filter_fields(self._filter_fields(self.current_spec().types))
        for chosen in f.fields:
            bar.set_field_filter(chosen.field, chosen)
        for tag_id in f.include:
            bar.add_tag(tag_id)
        for tag_id in f.exclude:
            bar.add_tag(tag_id, exclude=True)
        if f.text:
            bar.set_text(f.text)

    def current_spec(self, types: tuple[str, ...] | None = None) -> SearchSpec:
        """The page's spec with the filter bar applied, for ``types`` (default: the page's
        types, or the "Only" type). The last sort the user chose is kept when the listed
        types have its field; otherwise the page's default sort applies."""
        filters = self.filter_bar.filters()
        base = self._base
        within = base.within
        if types is None:
            types = (filters.only,) if filters.only is not None else base.types
            if filters.within is not None and filters.within_type is not None:
                within = filters.within
                types = self._types_within(types, filters.within_type)
        text = " ".join(t for t in (base.text, filters.text) if t)
        # Field filters apply where every listed type has the field (the others' chips
        # stay, greyed, until it does).
        filterable = {f.name for f in self._filter_fields(types)}
        fields = tuple(f for f in filters.fields if f.field in filterable)
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
            within=within,
            inherit_tags=filters.inherit_tags,
            aggregate_up=filters.aggregate_up,
            contents=filters.contents and searches_contents(self.session),
            fields=base.fields + fields,
            # In the tree, containers expand to show what they hold instead.
            show_contained=filters.show_contained and not self._tree_layout(),
            # ...and what a listed container holds isn't listed again at the top.
            nest=self._tree_layout(),
        )

    def _tree_layout(self) -> bool:
        return self.layout_mode == "tree" and not self.grouped

    def _filter_fields(self, types: Sequence[str]) -> list[FilterField]:
        """The fields the listed types can all be filtered on."""
        return [
            FilterField(f.name, f.spec.label, f.spec.search, f.type, f.spec.display)
            for f in scope_fields(self.session.schema, types)
            if f.spec.search is not None
        ]

    def _load_choices(self, name: str, on_done: Callable[[ChoiceCounts], None]) -> None:
        """A choice filter's values among the current results, in a worker."""
        spec, session = self.current_spec(), self.session

        def job() -> ChoiceCounts:
            fields = search_fields(session.schema, spec.types)
            with session.reader.connect() as conn:
                contents = session.contents_scope(conn, spec)
                tree = session.tag_cache.get()
                return choice_counts(conn, spec, tree, name, fields, contents=contents)

        def failed(error: BaseException) -> None:
            logger.warning("Couldn't list the values of %s: %s", name, error)
            on_done([])

        run_in_pool(job, on_done=on_done, on_error=failed, pool=self._pool)

    def _types_within(self, types: tuple[str, ...], container: str) -> tuple[str, ...]:
        """The types listed within a container: the page's own, where the container can
        hold them (Images within an artist), else everything it can hold (Artists within an
        artist would be empty). Search all keeps every type, so it stays grouped."""
        if not types:
            return types
        holds = contained_types(self.session.schema, container)
        kept = tuple(t for t in types if t in holds)
        return kept or tuple(holds)

    # --- drilling down (DESIGN.md §12) ---

    def show_within(self, entity_id: int, title: str, type_id: str) -> None:
        """List only what an entity contains: a "Within" chip, keeping the tag filters."""
        self.filter_bar.set_within(entity_id, title, type_id)

    def item_menu(self, hit: SearchHit) -> QMenu:
        """The right-click menu of one result."""
        menu = QMenu(self)
        open_action = menu.addAction("Open page")
        open_action.triggered.connect(lambda: self.open_requested.emit(hit.id))
        menu.setDefaultAction(open_action)  # bold: what double-click does
        within = menu.addAction("Show contents in search")
        holds = contained_types(self.session.schema, hit.type)
        within.setEnabled(bool(holds))
        within.setToolTip(
            f"List only what {hit.title} contains" if holds else "This item contains nothing"
        )
        within.triggered.connect(lambda: self.show_within(hit.id, hit.title, hit.type))
        kind = file_kind(self.session.schema, hit.type)
        if kind is not None and self.file_opener is not None:
            menu.addSeparator()
            open_file = add_file_actions(
                menu, self.file_opener, entity_id=hit.id, folder=kind == "folder"
            )
            if opens_file(self.session.schema, hit.type):
                menu.setDefaultAction(open_file)
        menu.addSeparator()
        add_reread_actions(menu, lambda replace: self._reread(hit, replace))
        for container in containers_for(self.session.schema, hit.type):  # Add to project…
            noun = entity_label(self.session.schema.by_type_id(container).entity).lower()
            put = menu.addAction(f"Add to {noun}\u2026")
            put.triggered.connect(
                lambda _=False, c=container: self._on_selection_or(
                    hit, lambda ids: self.add_to_requested.emit(c, ids)
                )
            )
        if writes_back(self.session, hit.type):
            write = menu.addAction("Write to file\u2026")
            write.setToolTip(WRITE_BACK_TIP)
            write.triggered.connect(
                lambda: self._on_selection_or(hit, self.write_back_requested.emit)
            )
        if self.session.theme.online_sources:
            look_up = menu.addAction("Look up online")
            look_up.setToolTip(LOOK_UP_TIP)
            look_up.triggered.connect(
                lambda: self._on_selection_or(hit, self.look_up_requested.emit)
            )
        theme_actions = actions_for(self.session.schema, hit.type)
        if theme_actions:
            menu.addSeparator()
            for theme_action in theme_actions:
                item = menu.addAction(theme_action.label)
                item.triggered.connect(
                    lambda _=False, m=theme_action.method: self._run_action(hit, m)
                )
        if self._menu_actions:
            menu.addSeparator()
            for label, callback in self._menu_actions:
                item = menu.addAction(label)
                item.triggered.connect(lambda _=False, c=callback: self._on_selection_or(hit, c))
        return menu

    def add_menu_action(self, label: str, callback: Callable[[list[int]], None]) -> None:
        """Add an entry to each result's right-click menu: ``callback`` gets the selected
        items if the clicked one is among them, else just that one (an embedded related
        search's "Remove from cast", #125)."""
        self._menu_actions.append((label, callback))

    def _on_selection_or(self, hit: SearchHit, callback: Callable[[list[int]], None]) -> None:
        self.selected_entity_ids(lambda ids: callback(ids if hit.id in ids else [hit.id]))

    def _run_action(self, hit: SearchHit, method: str) -> None:
        """Run an action on the selection if the item is in it (the action takes those it
        applies to), else just the item."""

        def chosen(ids: list[int]) -> None:
            self.action_requested.emit(method, ids if hit.id in ids else [hit.id])

        self.selected_entity_ids(chosen)

    def _reread(self, hit: SearchHit, replace: bool) -> None:
        """Re-read the selection if the item is in it, else just the item."""

        def chosen(ids: list[int]) -> None:
            self.reread_requested.emit(ids if hit.id in ids else [hit.id], replace)

        self.selected_entity_ids(chosen)

    def _table_menu(self, point: QPoint) -> None:
        hit = self.model.hit(self.table.indexAt(point).row())
        if hit is not None:
            self.item_menu(hit).exec(self.table.viewport().mapToGlobal(point))

    def showing_groups(self) -> bool:
        return self.results.currentWidget() is self.groups

    def refresh(self) -> None:
        """Run the search again (after a scan), keeping the results until new ones arrive,
        and reload the tag tree for the filter bar."""
        if self.grouped:
            self._run()
        else:
            self.model.refresh()
            self.tree_model.refresh_children()  # expanded containers' rows too
        self._load_tags()

    def show_all(self, type_id: str) -> None:
        """Narrow the global search to one type (a section's "Show all")."""
        self.filter_bar.set_only(type_id, self._plurals.get(type_id, type_id))

    def _run(self) -> None:
        self._generation += 1
        spec = self.current_spec()
        self.filter_bar.set_filter_fields(self._filter_fields(spec.types))
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
        self._show_layout_buttons()
        total = sum(g.count for g in groups)
        self.status.setText(count_text(total) if total else "Nothing found")

    def _show_list(self, spec: SearchSpec) -> None:
        self._apply_card_lines(spec.types)
        columns = list_columns(self.session.schema, spec.types, self.session.has_keywords)
        shows_contents = self._tree_layout() or spec.show_contained
        if shows_contents and not any(c.key == "type" for c in columns):
            # Contents can be of other types (an artist's images and fonts): say which.
            schema = self.session.schema
            if any(contained_types(schema, t.type_id) for t in scoped_tables(schema, spec.types)):
                columns.insert(1, ResultColumn("type", "Type", sortable=False))
        if columns != self.model.columns:
            self.model.set_search(spec, columns)
            set_column_widths(self.table, columns)
        elif spec != self.model.spec:
            self.model.set_search(spec)
        else:
            self.model.refresh()  # the same search again: keep the rows until new ones arrive
        self.table.set_columns(columns, self.hidden_columns, self.shown_columns)
        self.tree.set_columns_hidden(
            [c.key for c in columns], hidden_keys(columns, self.hidden_columns, self.shown_columns)
        )
        self._tree_column_widths()
        self.results.setCurrentWidget(self._layout_widget())
        self._show_layout_buttons()

    def _layout_widget(self) -> QWidget:
        return {"grid": self.grid, "tree": self.tree}.get(self.layout_mode, self.table)

    def _tree_column_widths(self) -> None:
        header = self.table.horizontalHeader()
        for i in range(self.model.columnCount()):
            self.tree.setColumnWidth(i, max(header.sectionSize(i), 60))
        title = next((i for i, c in enumerate(self.model.columns) if c.key == "title"), 0)
        self.tree.setColumnWidth(title, max(self.tree.columnWidth(title), 260))

    # --- the preview strip ---

    def set_preview_visible(self, visible: bool) -> None:
        self.preview.setVisible(visible)
        if visible:
            self._update_preview()

    def _update_preview(self) -> None:
        if self.preview.isHidden():
            return
        if self.results.currentWidget() is self.tree:
            hits = self.tree.selected_hits()
            self.preview.show_selection([h.id for h in hits])
            return
        rows = self._selected_rows()
        if len(rows) == 1:
            self.selected_entity_ids(self.preview.show_selection)
        else:
            self.preview.show_count(len(rows))

    def _selected_rows(self) -> list[int]:
        if self.showing_groups():
            return [
                i.row()
                for section in self.groups.sections
                for i in section.table.selectionModel().selectedRows()
            ]
        return [i.row() for i in self.table.selectionModel().selectedRows()]

    # --- layouts ---

    def _layout_button(self, kind: str, tip: str) -> QToolButton:
        button = QToolButton()
        button.setIcon(grid_icon(kind))
        button.setCheckable(True)
        button.setAutoRaise(True)
        button.setToolTip(tip)
        return button

    def set_layout(self, mode: str) -> None:
        """Show the list, grid, or tree (not reported: see :attr:`layout_changed`)."""
        if mode not in LAYOUTS:
            return
        was_tree = self._tree_layout()
        self.layout_mode = mode
        if was_tree != self._tree_layout():
            self._run()  # the tree's columns and contained items differ from the list's
        elif not self.showing_groups():
            self.results.setCurrentWidget(self._layout_widget())
        self._show_layout_buttons()
        self.selection_changed.emit()

    def _layout_clicked(self, mode: str) -> None:
        if mode != self.layout_mode:
            self.set_layout(mode)
            self.layout_changed.emit(mode)

    def _show_layout_buttons(self) -> None:
        grouped = self.showing_groups()
        self.list_button.setChecked(self.layout_mode == "list")
        self.grid_button.setChecked(self.layout_mode == "grid")
        self.tree_button.setChecked(self.layout_mode == "tree")
        for button in (self.list_button, self.grid_button, self.tree_button):
            button.setEnabled(not grouped)
        if grouped:
            self.grid_button.setToolTip("Search all shows sections; pick Show all for the grid")
        else:
            self.grid_button.setToolTip("Show the results as thumbnails")
        box = self.filter_bar.contained_box
        box.setEnabled(not self._tree_layout())
        box.setToolTip(
            "In the tree, expand an item to see what it contains"
            if self._tree_layout()
            else "Show contained items: also list everything inside the matching items (an "
            "artist's albums and songs)"
        )

    def set_thumbnail_size(self, size: int) -> None:
        self.grid.set_thumbnail_size(size)

    # --- card lines ---

    def card_lines(self, types: Sequence[str] | None = None) -> CardLines:
        """The lines under each type's card titles: the user's choice (the fields each type
        has) or, without one, the theme's ``card_lines``."""
        if types is None:
            types = self._types()
        lines: dict[str, list[tuple[str, str]]] = {}
        for table in scoped_tables(self.session.schema, types):
            fields = {f.name: f for f in table.fields}
            names = self._card_override
            if names is None:
                names = list(table.entity.card_lines)
            lines[table.type_id] = [(n, fields[n].spec.label) for n in names if n in fields]
        return lines

    def card_fields(self, types: Sequence[str] | None = None) -> list[tuple[str, str]]:
        """Every field a card line could show for the listed types, in declaration order."""
        found: dict[str, str] = {}
        for table in scoped_tables(self.session.schema, self._types() if types is None else types):
            for f in table.fields:
                if f.name not in CORE_FIELDS:
                    found.setdefault(f.name, f.spec.label)
        return list(found.items())

    def set_card_lines(self, names: Sequence[str] | None) -> None:
        """Show ``names`` under card titles (``None``: the theme's lines), and report it."""
        self._card_override = list(names) if names is not None else None
        self._apply_card_lines(self._types())
        self.model.refresh()  # load the fields the new lines show
        self.card_lines_changed.emit(self._card_override)

    def _types(self) -> tuple[str, ...]:
        spec = self.model.spec
        return spec.types if spec is not None else self.current_spec().types

    def _apply_card_lines(self, types: Sequence[str]) -> None:
        lines = self.card_lines(types)
        self.grid.displays = {
            f.name: f.spec.display
            for table in scoped_tables(self.session.schema, types)
            for f in table.fields
            if f.spec.display is not None
        }
        self.grid.set_card_lines(lines)
        self.model.set_extra_fields([key for shown in lines.values() for key, _ in shown])

    def _shown_card_fields(self) -> list[str]:
        shown: dict[str, None] = {}
        for lines in self.card_lines().values():
            for key, _ in lines:
                shown[key] = None
        return list(shown)

    def grid_menu(self, hit: SearchHit | None = None) -> QMenu:
        """The grid's context menu: the card's own actions (when on one), which fields show
        under card titles, and sizes."""
        menu = self.item_menu(hit) if hit is not None else QMenu(self)
        if hit is not None:
            menu.addSeparator()
        heading = menu.addAction("Card lines")
        heading.setEnabled(False)
        shown = self._shown_card_fields()
        for key, label in self.card_fields():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key in shown)
            action.toggled.connect(lambda on, key=key: self._toggle_card_line(key, on))
        theme_lines = menu.addAction("Use the theme's card lines")
        theme_lines.setEnabled(self._card_override is not None)
        theme_lines.triggered.connect(lambda: self.set_card_lines(None))
        if self.size_menu is not None:
            menu.addSeparator()
            menu.addMenu(self.size_menu)
        return menu

    def _toggle_card_line(self, key: str, on: bool) -> None:
        names = [n for n in self._shown_card_fields() if n != key]
        if on:
            order = [k for k, _ in self.card_fields()]
            names = sorted([*names, key], key=lambda n: order.index(n) if n in order else 0)
        self.set_card_lines(names)

    def _grid_menu(self, point: QPoint, row: int) -> None:
        self.grid_menu(self.model.hit(row) if row >= 0 else None).exec(point)

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
            self.shown_columns.add(key)
        else:
            self.hidden_columns.add(key)
            self.shown_columns.discard(key)
        columns = self.table.columns
        self.table.set_columns(columns, self.hidden_columns, self.shown_columns)
        self.tree.set_columns_hidden(
            [c.key for c in columns], hidden_keys(columns, self.hidden_columns, self.shown_columns)
        )
        self.groups.set_hidden_columns(self.hidden_columns, self.shown_columns)
        self.hidden_columns_changed.emit(sorted(self.hidden_columns))
        self.shown_columns_changed.emit(sorted(self.shown_columns))

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
        every section; or in the tree). Selected rows that aren't loaded yet are looked up in
        a worker."""
        if self.results.currentWidget() is self.tree:
            on_done([h.id for h in self.tree.selected_hits()])
        elif self.showing_groups():
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

    def _activated(self, index: QModelIndex, alternate: bool = False) -> None:
        hit = self.model.hit(index.row()) if index.isValid() else None
        if hit is not None:
            self.activate(hit, alternate)

    def _tree_activated(self, index: QModelIndex, alternate: bool = False) -> None:
        hit = self.tree_model.hit(index) if index.isValid() else None
        if hit is not None:
            self.activate(hit, alternate)

    def activate(self, hit: SearchHit, alternate: bool = False) -> None:
        """Double-click or Enter (``alternate`` for Ctrl+Enter): open the item's page, or
        its file for types whose ``double_click`` is ``"open_file"``; Ctrl+Enter does the
        other."""
        wants_file = opens_file(self.session.schema, hit.type) != alternate
        if wants_file and self.file_opener is not None and file_kind(self.session.schema, hit.type):
            self.file_opener.open_entity(hit.id)
        else:
            self.open_requested.emit(hit.id)

    def _tree_menu(self, point: QPoint) -> None:
        hit = self.tree_model.hit(self.tree.indexAt(point))
        if hit is not None:
            self.item_menu(hit).exec(self.tree.viewport().mapToGlobal(point))

    def _filters_changed(self, filters: Filters) -> None:
        toggles = {
            "show_contained": filters.show_contained,
            "inherit_tags": filters.inherit_tags,
            "aggregate_up": filters.aggregate_up,
            "contents": filters.contents,
        }
        if toggles != self._toggles:
            self._toggles = toggles
            self.toggles_changed.emit(dict(toggles))
        self._run()

    # --- the list's count and sorting ---

    def _counted(self, total: int) -> None:
        self.status.setText(count_text(total) if total else "Nothing found")

    def _failed(self, message: str) -> None:
        self.status.setText(message)

    def _sort_clicked(self, column: int, order: Qt.SortOrder) -> None:
        if self._showing_sort:
            return
        self.model.sort(column, order)
        if self.model.spec is not None:
            self._sort = self.model.spec.sort  # kept when the filters change
        self._show_sort()  # a column that can't sort puts the indicator back

    def _show_sort(self) -> None:
        current = self.model.sort_column()
        self._showing_sort = True
        try:
            for header in (self.table.horizontalHeader(), self.tree.header()):
                if current is None:
                    header.setSortIndicator(-1, header.sortIndicatorOrder())
                else:
                    header.setSortIndicator(*current)
        finally:
            self._showing_sort = False
        if self.model.searching:
            self.status.setText("Searching…")
