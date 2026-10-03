"""Builds SQLAlchemy queries from a ``SearchSpec`` (DESIGN.md §8).

Everything is expressed with SQLAlchemy constructs (AGENTS.md rule 10). Tag groups come from a
:class:`~tagalot.core.tags.TagTree` snapshot, so "this tag or any descendant" is expanded in
memory and the SQL only sees plain id lists.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Connection,
    Select,
    and_,
    func,
    literal_column,
    or_,
    select,
    true,
    union,
)
from sqlalchemy.orm import InstrumentedAttribute

from tagalot.core.models import (
    Entity,
    EntityAncestor,
    EntityContains,
    EntityMerge,
    EntityTag,
    entity_fts,
)
from tagalot.core.search_spec import (
    ChoiceFilter,
    FieldFilter,
    RangeFilter,
    SearchSpec,
    SortKey,
    TextFilter,
    TextMatch,
)
from tagalot.core.tags import TagTree
from tagalot.core.triage import triage_condition

MIN_TRIGRAM = 3
"""Terms shorter than this can't use the trigram index and fall back to ``LIKE`` (§8)."""

CORE_FIELDS: Mapping[str, ColumnElement[Any]] = {
    "title": Entity.title.expression,
    "created_at": Entity.created_at.expression,
    "updated_at": Entity.updated_at.expression,
}
"""Fields every entity has. ``search_fields.search_fields`` adds the theme's."""

IdColumn = ColumnElement[int] | InstrumentedAttribute[int]
"""The entity id a condition tests: the listed entity, a descendant, …"""

_WHITESPACE = re.compile(r"\s+")


class SearchError(ValueError):
    """A search can't be run as specified; the message is suitable for showing to the user."""


@dataclass(frozen=True)
class SearchHit:
    id: int
    type: str
    title: str


def build_query(
    spec: SearchSpec,
    tree: TagTree,
    fields: Mapping[str, ColumnElement[Any]] = CORE_FIELDS,
) -> Select[int, str, str]:
    """Return a ``SELECT id, type, title`` for ``spec``, sorted, without paging."""
    query = select(Entity.id, Entity.type, Entity.title).where(_Filter(spec, tree, fields).result())
    return query.order_by(*_order_by(spec, fields))


def text_ids(text: str) -> Select[int]:
    """Ids of entities whose ``entity_fts`` row matches the search box text (§8).

    The text is split on whitespace and every term must match somewhere in the title or body.
    Terms of three or more characters are quoted trigram ``MATCH`` terms, so quotes, ``AND``,
    and ``*`` in the input are ordinary characters. Shorter terms use ``LIKE`` (with ``%``
    and ``_`` escaped), because the trigram index can't match them.
    """
    terms = [t for t in _WHITESPACE.split(text.strip()) if t]
    long_terms = [t for t in terms if len(t) >= MIN_TRIGRAM]
    short_terms = [t for t in terms if len(t) < MIN_TRIGRAM]
    matching = select(entity_fts.c.rowid)
    if long_terms:
        query = " ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
        matching = matching.where(literal_column(entity_fts.name).match(query))
    for term in short_terms:
        matching = matching.where(
            or_(
                entity_fts.c.title.icontains(term, autoescape=True),
                entity_fts.c.body.icontains(term, autoescape=True),
            )
        )
    return matching


def run_search(
    conn: Connection,
    spec: SearchSpec,
    tree: TagTree,
    *,
    offset: int = 0,
    limit: int | None = 100,
    fields: Mapping[str, ColumnElement[Any]] = CORE_FIELDS,
) -> list[SearchHit]:
    """One page of results, in sort order."""
    query = build_query(spec, tree, fields).offset(offset).limit(limit)
    return [SearchHit(id, type_, title) for id, type_, title in conn.execute(query)]


def count_matches(
    conn: Connection,
    spec: SearchSpec,
    tree: TagTree,
    fields: Mapping[str, ColumnElement[Any]] = CORE_FIELDS,
) -> int:
    """How many entities the search lists (for "123 results")."""
    query = select(func.count()).select_from(Entity).where(_Filter(spec, tree, fields).result())
    return int(conn.scalar(query) or 0)


def count_by_type(
    conn: Connection,
    spec: SearchSpec,
    tree: TagTree,
    fields: Mapping[str, ColumnElement[Any]] = CORE_FIELDS,
) -> dict[str, int]:
    """How many entities of each type the search lists (the global search's sections, §8).

    Types with no matches are absent. One query, however many types there are.
    """
    query = (
        select(Entity.type, func.count())
        .where(_Filter(spec, tree, fields).result())
        .group_by(Entity.type)
    )
    return {type_: int(n) for type_, n in conn.execute(query)}


# --- Building the filter (DESIGN.md §8 "Semantics") ---


class _Filter:
    """Builds one search's ``WHERE`` clause (DESIGN.md §8 "Semantics").

    Conditions take the id column they test, so the same logic serves the listed entity, one
    of its descendants (aggregation), or its ancestors (inheritance). Each distinct set (a tag
    group, the exclusion set, the text matches) becomes one ``MATERIALIZED`` CTE, computed
    once per query however many conditions use it, and every test against a set is an
    uncorrelated ``IN``, so SQLite builds the set from an index instead of probing per row.
    """

    def __init__(
        self, spec: SearchSpec, tree: TagTree, fields: Mapping[str, ColumnElement[Any]]
    ) -> None:
        self.spec = spec
        self.tree = tree
        self.fields = fields
        self._sets: dict[object, Select[int]] = {}
        excluded = frozenset().union(*(_subtree(tree, t) for t in spec.exclude))
        self._excluded = excluded

    def result(self) -> ColumnElement[bool]:
        """Which entities the search lists."""
        matches = self._matches()
        if self.spec.nest and not self.spec.show_contained:
            # Drop matches held by another match: expanding that one shows them.
            matched = self._materialize(("matched",), select(Entity.id).where(matches))
            held = select(EntityAncestor.entity_id).where(
                EntityAncestor.ancestor_id.in_(matched), EntityAncestor.depth > 0
            )
            return and_(matches, Entity.id.not_in(held))
        if not self.spec.show_contained:
            return matches
        # Matches plus every descendant of a match, whatever its type; descendants still
        # have to pass exclusion.
        matched = self._materialize(("matched",), select(Entity.id).where(matches))
        contained = select(EntityAncestor.entity_id).where(
            EntityAncestor.ancestor_id.in_(matched), EntityAncestor.depth > 0
        )
        return and_(
            or_(Entity.id.in_(matched), Entity.id.in_(contained)),
            self._not_excluded(Entity.id),
        )

    def _matches(self) -> ColumnElement[bool]:
        """Entities that match in their own right (or via a descendant, with aggregate_up)."""
        spec = self.spec
        where: list[ColumnElement[bool]] = []
        if spec.types:
            where.append(Entity.type.in_(spec.types))
        if spec.within is not None:
            # A saved search within an item merged since follows it (§13).
            within = func.coalesce(
                select(EntityMerge.into_id)
                .where(EntityMerge.merged_id == spec.within)
                .scalar_subquery(),
                spec.within,
            )
            where.append(
                Entity.id.in_(
                    select(EntityAncestor.entity_id).where(
                        EntityAncestor.ancestor_id == within, EntityAncestor.depth > 0
                    )
                )
            )
        if spec.related is not None:
            name, other = spec.related
            a, b = self.fields.get(f"@{name}.a"), self.fields.get(f"@{name}.b")
            if a is None or b is None:
                raise SearchError(f"No relationship {name!r} for this search.")
            # From either side: the b items of an a, and the a items of a b.
            where.append(
                Entity.id.in_(union(select(b).where(a == other), select(a).where(b == other)))
            )
        where.append(self._not_excluded(Entity.id))
        if spec.triage is not None:
            where.append(triage_condition(spec.triage, spec.inherit_tags))
        # Field filters always apply to the listed entity: they belong to its type.
        where.extend(_field_condition(f, self.fields) for f in spec.fields)
        own = self._positive(Entity.id)
        if spec.aggregate_up and own:
            # Tags and text may instead be matched by a (non-excluded) descendant. Find those
            # entities first (a small set), then their ancestors via the closure's key.
            matching = self._materialize(
                ("descendant matches",),
                select(Entity.id).where(*own, self._not_excluded(Entity.id)),
            )
            containers = select(EntityAncestor.ancestor_id).where(
                EntityAncestor.entity_id.in_(matching), EntityAncestor.depth > 0
            )
            where.append(or_(and_(*own), Entity.id.in_(containers)))
        else:
            where.extend(own)
        return and_(true(), *where)

    def _positive(self, entity_id: IdColumn) -> list[ColumnElement[bool]]:
        """Include groups and text, for the entity in ``entity_id``."""
        conditions: list[ColumnElement[bool]] = [
            entity_id.in_(self._tagged(_subtree(self.tree, tag))) for tag in self.spec.include
        ]
        if self.spec.text:
            text = self.spec.text
            conditions.append(entity_id.in_(self._materialize(("text", text), text_ids(text))))
        return conditions

    def _not_excluded(self, entity_id: IdColumn) -> ColumnElement[bool]:
        if not self._excluded:
            return true()
        return entity_id.not_in(self._tagged(self._excluded))

    def _tagged(self, tag_ids: frozenset[int]) -> Select[int]:
        """Ids of entities carrying any of ``tag_ids``: directly, or with inheritance also
        via any ancestor (the closure's depth-0 self row covers an entity's own tags)."""
        if not self.spec.inherit_tags:
            query = select(EntityTag.entity_id).where(EntityTag.tag_id.in_(tag_ids))
        else:
            query = (
                select(EntityAncestor.entity_id)
                .join(EntityTag, EntityTag.entity_id == EntityAncestor.ancestor_id)
                .where(EntityTag.tag_id.in_(tag_ids))
            )
        return self._materialize(("tags", tag_ids), query)

    def _materialize(self, key: object, query: Select[int]) -> Select[int]:
        """``SELECT id FROM <one MATERIALIZED CTE per distinct key>``."""
        if key not in self._sets:
            cte = query.cte(f"set{len(self._sets)}").prefix_with("MATERIALIZED")
            self._sets[key] = select(cte.c[0])
        return self._sets[key]


def _subtree(tree: TagTree, tag_id: int) -> frozenset[int]:
    """A tag's subtree; a tag no longer in the tree (e.g. from a saved search) is just itself,
    so including it matches nothing and excluding it excludes nothing."""
    return tree.descendants(tag_id) if tag_id in tree else frozenset({tag_id})


def _column(name: str, fields: Mapping[str, ColumnElement[Any]]) -> ColumnElement[Any]:
    try:
        return fields[name]
    except KeyError:
        raise SearchError(f"Unknown field {name!r} for this search.") from None


def _field_condition(
    f: FieldFilter, fields: Mapping[str, ColumnElement[Any]]
) -> ColumnElement[bool]:
    column = _column(f.field, fields)
    match f:
        case TextFilter(text=text, match=TextMatch.STARTS_WITH):
            return column.istartswith(text, autoescape=True)
        case TextFilter(text=text):
            return column.icontains(text, autoescape=True)
        case RangeFilter(low=low, high=high):
            bounds: list[ColumnElement[bool]] = []
            if low is not None:
                bounds.append(column >= low)
            if high is not None:
                bounds.append(column <= high)
            # Both ends open: any value, but not a missing one.
            return and_(*bounds) if bounds else column.is_not(None)
        case ChoiceFilter(values=values):
            return column.in_(values)


def _order_by(
    spec: SearchSpec, fields: Mapping[str, ColumnElement[Any]]
) -> Sequence[ColumnElement[Any]]:
    keys: list[ColumnElement[Any]] = []
    for key in spec.sort:
        column = _column(key.field, fields)
        if key.field == "title":
            column = column.collate("NOCASE")
        keys.append(column.desc() if key.descending else column.asc())
    keys.append(Entity.id.asc())  # stable order, so paging never skips or repeats
    return keys


MAX_CHILDREN = 1000
"""Children listed under one container in the tree layout."""


def child_hits(
    conn: Connection,
    spec: SearchSpec,
    tree: TagTree,
    parent_id: int,
    *,
    limit: int = MAX_CHILDREN,
    sort: tuple[SortKey, ...] = (SortKey("title"),),
    fields: Mapping[str, ColumnElement[Any]] = CORE_FIELDS,
) -> list[SearchHit]:
    """What a container directly holds, for the tree layout (DESIGN.md §8 "Show
    contained"): every type, in ``sort`` order (the container's ``contents_sort``, with
    ``fields`` from :func:`~tagalot.core.search_fields.contents_order`), not re-filtered by
    the search's includes, text, or fields (they are shown because of their container), but
    still without excluded items."""
    children = (
        select(EntityContains.child_id)
        .where(EntityContains.parent_id == parent_id)
        .scalar_subquery()
    )
    listing = SearchSpec(
        exclude=spec.exclude,
        inherit_tags=spec.inherit_tags,
        within=parent_id,
        sort=sort,
    )
    query = build_query(listing, tree, fields).where(Entity.id.in_(children)).limit(limit)
    return [SearchHit(id, type_, title) for id, type_, title in conn.execute(query)]


MAX_CHOICES = 500
"""Values offered for one choice filter."""


def choice_counts(
    conn: Connection,
    spec: SearchSpec,
    tree: TagTree,
    name: str,
    fields: Mapping[str, ColumnElement[Any]],
    *,
    limit: int = MAX_CHOICES,
) -> list[tuple[Any, int]]:
    """The values of field ``name`` among the search's results, with how many results have
    each, most common first. The search's own filter on ``name`` is left out, so the counts
    show what adding another value would bring. Empty values aren't listed."""
    column = fields.get(name)
    if column is None:
        raise SearchError(f"No field {name!r} for these types")
    others = replace(spec, fields=tuple(f for f in spec.fields if f.field != name), sort=())
    count = func.count()
    query = (
        select(column.label("value"), count)
        .select_from(Entity)
        .where(_Filter(others, tree, fields).result(), column.is_not(None))
        .group_by(column)
        .order_by(count.desc(), column)
        .limit(limit)
    )
    return [(value, int(n)) for value, n in conn.execute(query)]
