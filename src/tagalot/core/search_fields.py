"""Theme fields in searches and result lists (DESIGN.md §8, §12).

- :func:`scope_fields` lists the fields every type in a search's scope has; they can be
  filtered, sorted, and shown as list columns.
- :func:`search_fields` maps them (plus the core fields) to SQL expressions for
  :mod:`tagalot.core.search`.
- :func:`field_values` loads the values shown for one page of results.
- :func:`view_spec` turns a theme's :class:`~tagalot.themes.api.SearchView` into a
  :class:`~tagalot.core.search_spec.SearchSpec`.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import ColumnElement, Connection, func, select

from tagalot.core.models import Entity
from tagalot.core.search import CORE_FIELDS, SearchHit
from tagalot.core.search_spec import SearchSpec, SortKey
from tagalot.core.theme_schema import EntityTable, ThemeSchema
from tagalot.themes.api import FieldInfo, SearchView, entity_label, entity_plural


def contained_types(schema: ThemeSchema, type_id: str) -> list[str]:
    """The types an entity of ``type_id`` can contain, at any depth (an artist: albums
    and songs), in the theme's type order."""
    theme = schema.theme
    edges = [(theme.type_id_of(c.parent), theme.type_id_of(c.child)) for c in theme.containment]
    found: set[str] = set()
    frontier = [type_id]
    while frontier:
        current = frontier.pop()
        for parent, child in edges:
            if parent == current and child not in found:
                found.add(child)
                frontier.append(child)
    return [t.type_id for t in schema.entities.values() if t.type_id in found]


def contents_order(
    schema: ThemeSchema, type_id: str
) -> tuple[tuple[SortKey, ...], dict[str, ColumnElement[Any]]]:
    """How an entity of ``type_id`` orders what it holds (its type's ``contents_sort``,
    else by title), with the field expressions that sort needs: a field any contained type
    has, ``NULL`` for the others."""
    try:
        entity = schema.by_type_id(type_id).entity
    except KeyError:
        return (SortKey("title"),), dict(CORE_FIELDS)
    keys = tuple(SortKey(k.field, k.descending) for k in entity.contents_sort)
    if not keys:
        return (SortKey("title"),), dict(CORE_FIELDS)
    fields: dict[str, ColumnElement[Any]] = dict(CORE_FIELDS)
    tables = scoped_tables(schema, contained_types(schema, type_id))
    for key in keys:
        if key.field in fields:
            continue
        values = [
            select(t.table.c[key.field]).where(t.table.c.id == Entity.id).scalar_subquery()
            for t in tables
            if key.field in t.table.c
        ]
        if values:
            fields[key.field] = values[0] if len(values) == 1 else func.coalesce(*values)
    return tuple(k for k in keys if k.field in fields) or (SortKey("title"),), fields


def scoped_tables(schema: ThemeSchema, types: Sequence[str]) -> list[EntityTable]:
    """The entity tables a search covers: those of ``types``, or every type if it is empty.

    Unknown type ids are skipped (a saved search may name a type the theme has dropped).
    """
    tables = list(schema.entities.values())
    if not types:
        return tables
    return [t for type_id in dict.fromkeys(types) for t in tables if t.type_id == type_id]


def scope_fields(schema: ThemeSchema, types: Sequence[str]) -> list[FieldInfo]:
    """Fields that every scoped type declares, with the same value type, in the first
    type's declaration order. Fields named like a core field (``title``…) are left out."""
    tables = scoped_tables(schema, types)
    if not tables:
        return []
    common = []
    for f in tables[0].fields:
        if f.name in CORE_FIELDS:
            continue
        if all(any(g.name == f.name and g.type is f.type for g in t.fields) for t in tables[1:]):
            common.append(f)
    return common


def search_fields(schema: ThemeSchema, types: Sequence[str]) -> dict[str, ColumnElement[Any]]:
    """The core fields plus the scope's theme fields, as expressions on the listed entity.

    A theme field is a scalar subquery on its type's table (``NULL`` for entities of other
    types); with several scoped types they are combined with ``coalesce``. The subqueries
    look rows up by primary key, so filtering and sorting on them needs no join.

    Each relationship's link table columns come too, as ``@<name>.a`` and ``@<name>.b``
    (not field names), for :attr:`SearchSpec.related`.
    """
    tables = scoped_tables(schema, types)
    fields: dict[str, ColumnElement[Any]] = dict(CORE_FIELDS)
    for f in scope_fields(schema, types):
        values = [
            select(t.table.c[f.name]).where(t.table.c.id == Entity.id).scalar_subquery()
            for t in tables
        ]
        fields[f.name] = values[0] if len(values) == 1 else func.coalesce(*values)
    for name, link in schema.relationships.items():
        fields[f"@{name}.a"] = link.table.c.a_id
        fields[f"@{name}.b"] = link.table.c.b_id
    return fields


def field_values(
    conn: Connection, schema: ThemeSchema, hits: Iterable[SearchHit], names: Sequence[str]
) -> dict[int, dict[str, Any]]:
    """``{entity id: {field: value}}`` for ``names`` on the given hits (one query per type).

    Entities whose type has none of the fields (or isn't the theme's) get an empty dict.
    """
    by_type: dict[str, list[int]] = {}
    for hit in hits:
        by_type.setdefault(hit.type, []).append(hit.id)
    values: dict[int, dict[str, Any]] = {}
    for type_id, ids in by_type.items():
        for entity_id in ids:
            values[entity_id] = {}
        try:
            table = schema.by_type_id(type_id).table
        except KeyError:
            continue
        columns = [table.c[n] for n in names if n in table.c]
        if not columns:
            continue
        for row in conn.execute(select(table.c.id, *columns).where(table.c.id.in_(ids))):
            values[row[0]] = {c.name: v for c, v in zip(columns, row[1:], strict=True)}
    return values


def view_spec(schema: ThemeSchema, view: SearchView) -> SearchSpec:
    """The search a theme view starts with: its types, toggles, and default sort."""
    theme = schema.theme
    return SearchSpec(
        types=tuple(theme.type_id_of(e) for e in view.types),
        inherit_tags=view.inherit_tags,
        show_contained=view.show_contained,
        sort=tuple(SortKey(s.field, s.descending) for s in view.default_sort),
    )


def type_labels(schema: ThemeSchema) -> Mapping[str, str]:
    """``{type id: display name}`` for the theme's entity types ("Album")."""
    return {t.type_id: entity_label(t.entity) for t in schema.entities.values()}


def type_plurals(schema: ThemeSchema) -> Mapping[str, str]:
    """``{type id: plural display name}`` for the theme's entity types ("Albums")."""
    return {t.type_id: entity_plural(t.entity) for t in schema.entities.values()}
