"""Items side by side, for the Dedupe page's compare pane (DESIGN.md §12 "Dedupe", §13;
#119).

:func:`compare_items` reads a few items in one read transaction, in a worker, and lines up
what the pane shows: a row per fact (the type, the name, the theme's fields, the user's own
fields, tags, contents, and the files' format, size, and places), a value per item, and
whether the values differ.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, func, select

from tagalot.core.detail import PATH_MARK, role_files
from tagalot.core.formats import format_bytes, format_value
from tagalot.core.models import Entity, EntityContains
from tagalot.core.tags import TagTree, entity_tags
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import entity_fields, entity_label

MAX_ITEMS = 6
"""Items compared at once; the pane says how many more there are."""

MAX_FILES = 5
"""Files listed per item; the cell says how many more there are."""


@dataclass(frozen=True)
class CompareItem:
    """One column: an item being compared."""

    id: int
    title: str
    type_label: str


@dataclass(frozen=True)
class CompareRow:
    """One fact about every item, as text (``""`` when an item has none)."""

    key: str
    """Stable for tests and styling: ``type``, ``title``, ``field:<name>``,
    ``extra:<name>``, ``tags``, ``contents``, ``format``, ``size``, ``files``."""
    label: str
    values: tuple[str, ...]

    @property
    def differs(self) -> bool:
        """The items don't all have the same value."""
        return len(set(self.values)) > 1


@dataclass(frozen=True)
class Comparison:
    items: tuple[CompareItem, ...]
    rows: tuple[CompareRow, ...]
    more: int = 0
    """Items asked for but not shown (past :data:`MAX_ITEMS`)."""
    versions: bool = False
    """The items are of one type whose main role holds several files, so merging keeps
    every file as a version (#121)."""

    def row(self, key: str) -> CompareRow | None:
        return next((r for r in self.rows if r.key == key), None)


def compare_items(
    conn: Connection,
    schema: ThemeSchema,
    entity_ids: Sequence[int],
    root_path: Callable[[str], str | None],
) -> Comparison:
    """The items, side by side, in the order given (duplicates and items that no longer
    exist are left out). ``root_path`` gives this machine's path for a root id."""
    wanted = list(dict.fromkeys(entity_ids))
    found = {
        row.id: row
        for row in conn.execute(
            select(Entity.id, Entity.type, Entity.title, Entity.extra).where(Entity.id.in_(wanted))
        )
    }
    present = [i for i in wanted if i in found]
    shown, more = present[:MAX_ITEMS], max(0, len(present) - MAX_ITEMS)
    items: list[CompareItem] = []
    columns: list[dict[str, str]] = []
    labels: dict[str, str] = {}  # row key -> label, in first-seen order

    def put(column: dict[str, str], key: str, label: str, value: str) -> None:
        labels.setdefault(key, label)
        column[key] = value

    tree = TagTree.load(conn)
    tags = entity_tags(conn, shown)
    for entity_id in shown:
        row = found[entity_id]
        column: dict[str, str] = {}
        try:
            table = schema.by_type_id(row.type)
        except KeyError:  # a type the theme no longer has
            entity, type_label = None, row.type
        else:
            entity, type_label = table.entity, entity_label(table.entity)
        items.append(CompareItem(entity_id, row.title, type_label))
        put(column, "type", "Type", type_label)
        put(column, "title", entity.title_label if entity else "Title", row.title)
        if entity is not None:
            shown_fields = [f for f in entity_fields(entity) if f.spec.detail]
            if shown_fields:
                values = conn.execute(
                    select(*(table.table.c[f.name] for f in shown_fields)).where(
                        table.table.c.id == entity_id
                    )
                ).first()
                for i, f in enumerate(shown_fields):
                    value = values[i] if values is not None else None
                    put(column, f"field:{f.name}", f.spec.label, _text(value, f.spec.display))
        for name, value in sorted((row.extra or {}).items(), key=lambda kv: kv[0].casefold()):
            put(column, f"extra:{name}", name, _text(value, None))
        names = sorted(
            (tree.display_name(t) for t in tags.get(entity_id, ()) if t in tree),
            key=str.casefold,
        )
        put(column, "tags", "Tags", ", ".join(names))
        columns.append(column)

    _contents(conn, schema, shown, found, columns, put)
    _files(conn, schema, shown, found, columns, root_path, put)
    # A file row the theme already gives (assets2d's images have a Size field) isn't repeated.
    theirs = {labels[k].casefold() for k in labels if k.startswith("field:")}
    keys = [k for k in labels if k not in FILE_ROWS or labels[k].casefold() not in theirs]
    rows = tuple(
        CompareRow(key, labels[key], tuple(column.get(key, "") for column in columns))
        for key in keys
    )
    return Comparison(tuple(items), rows, more, _versions(schema, [found[i].type for i in shown]))


def _versions(schema: ThemeSchema, types: list[str]) -> bool:
    if len(types) < 2 or len(set(types)) > 1:
        return False
    try:
        entity = schema.by_type_id(types[0]).entity
    except KeyError:
        return False
    primary = next((r for r in entity.roles if r.primary), None)
    return primary is not None and primary.many


FILE_ROWS = ("format", "size")


def _text(value: Any, display: str | None) -> str:
    """A value as the pages show it."""
    formatted = format_value(value, display)
    if formatted is not None:
        return formatted
    match value:
        case None:
            return ""
        case bool():
            return "Yes" if value else "No"
        case datetime():
            return value.astimezone().strftime("%Y-%m-%d %H:%M")
        case date():
            return value.isoformat()
        case _:
            return str(value)


Put = Callable[[dict[str, str], str, str, str], None]


def _contents(
    conn: Connection,
    schema: ThemeSchema,
    shown: list[int],
    found: dict[int, Any],
    columns: list[dict[str, str]],
    put: Put,
) -> None:
    """How many items each contains, when any of them can contain items."""
    parents = {c.parent for c in schema.theme.containment}
    containers = [i for i in shown if any(_type_id_of(schema, p) == found[i].type for p in parents)]
    if not containers:
        return
    counts = dict(
        conn.execute(
            select(EntityContains.parent_id, func.count())
            .where(EntityContains.parent_id.in_(shown))
            .group_by(EntityContains.parent_id)
        ).all()
    )
    for entity_id, column in zip(shown, columns, strict=True):
        n = int(counts.get(entity_id, 0))
        put(column, "contents", "Contains", "1 item" if n == 1 else f"{n:,} items")


def _type_id_of(schema: ThemeSchema, entity: Any) -> str | None:
    table = schema.entities.get(entity)
    return table.type_id if table is not None else None


def _files(
    conn: Connection,
    schema: ThemeSchema,
    shown: list[int],
    found: dict[int, Any],
    columns: list[dict[str, str]],
    root_path: Callable[[str], str | None],
    put: Put,
) -> None:
    """Each item's main files (its primary role's, or every role's without one): their
    formats, total size, and where they are (folders too: an album's is where it is)."""
    for entity_id, column in zip(shown, columns, strict=True):
        role: str | None = None
        try:
            entity = schema.by_type_id(found[entity_id].type).entity
        except KeyError:
            pass
        else:
            primary = next((r for r in entity.roles if r.primary), None)
            role = primary.name if primary is not None else None
        linked = role_files(conn, entity_id, role, root_path)
        files = [f for f in linked if f.kind == "file"]
        formats = sorted({_format(f.relpath) for f in files} - {""})
        put(column, "format", "Format", ", ".join(formats))
        sizes = [f.size for f in files if f.size is not None]
        put(column, "size", "Size", format_bytes(sum(sizes)) if sizes else "")
        places = [
            f"{f.root_name} {PATH_MARK} {f.relpath}" if f.relpath else f.root_name
            for f in linked[:MAX_FILES]
        ]
        if len(linked) > MAX_FILES:
            places.append(f"and {len(linked) - MAX_FILES:,} more")
        put(column, "files", "Files", "\n".join(places))


def _format(relpath: str) -> str:
    name = relpath.rpartition("/")[2]
    stem, dot, ext = name.rpartition(".")
    return ext.upper() if dot and stem else ""
