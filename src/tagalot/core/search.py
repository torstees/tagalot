"""Builds SQLAlchemy queries from a ``SearchSpec`` (DESIGN.md §8).

Everything is expressed with SQLAlchemy constructs (AGENTS.md rule 10). Tag groups come from a
:class:`~tagalot.core.tags.TagTree` snapshot, so "this tag or any descendant" is expanded in
memory and the SQL only sees plain id lists.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Connection,
    Select,
    and_,
    exists,
    func,
    literal_column,
    or_,
    select,
    true,
)
from sqlalchemy.orm import InstrumentedAttribute, aliased

from tagalot.core.models import Entity, EntityAncestor, EntityTag, entity_fts
from tagalot.core.search_spec import (
    ChoiceFilter,
    FieldFilter,
    RangeFilter,
    SearchSpec,
    TextFilter,
    TextMatch,
)
from tagalot.core.tags import TagTree

MIN_TRIGRAM = 3
"""Terms shorter than this can't use the trigram index and fall back to ``LIKE`` (§8)."""

CORE_FIELDS: Mapping[str, ColumnElement[Any]] = {
    "title": Entity.title.expression,
    "created_at": Entity.created_at.expression,
    "updated_at": Entity.updated_at.expression,
}
"""Fields every entity has. Themes add their own columns to this mapping (M4, M11)."""

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
    query = select(Entity.id, Entity.type, Entity.title).where(_result_filter(spec, tree, fields))
    return query.order_by(*_order_by(spec, fields))


def text_condition(text: str, entity_id: IdColumn | None = None) -> ColumnElement[bool]:
    """Match the search box text against ``entity_fts`` (§8), for ``entity_id``
    (default: ``entity.id``).

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
    return (entity_id if entity_id is not None else Entity.id).in_(matching)


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
    query = select(func.count()).select_from(Entity).where(_result_filter(spec, tree, fields))
    return int(conn.scalar(query) or 0)


# --- Building the filter (DESIGN.md §8 "Semantics") ---
#
# Tag and text conditions take the id column they test, so the same logic can test the
# listed entity itself, one of its descendants (aggregate_up), or its ancestors (inheritance).


def _result_filter(
    spec: SearchSpec, tree: TagTree, fields: Mapping[str, ColumnElement[Any]]
) -> ColumnElement[bool]:
    """Which entities the search lists."""
    matches = _match_filter(spec, tree, fields)
    if not spec.show_contained:
        return matches
    # Matches plus every descendant of a match, whatever its type; descendants still have to
    # pass exclusion. The matches are computed in a CTE so they aren't re-evaluated per row.
    matched = select(Entity.id).where(matches).cte("matched")
    contained = exists().where(
        EntityAncestor.entity_id == Entity.id,
        EntityAncestor.depth > 0,
        EntityAncestor.ancestor_id.in_(select(matched.c.id)),
    )
    return and_(
        or_(Entity.id.in_(select(matched.c.id)), contained),
        _not_excluded(spec, tree, Entity.id),
    )


def _match_filter(
    spec: SearchSpec, tree: TagTree, fields: Mapping[str, ColumnElement[Any]]
) -> ColumnElement[bool]:
    """Entities that match in their own right (or via a descendant, with aggregate_up)."""
    where: list[ColumnElement[bool]] = []
    if spec.types:
        where.append(Entity.type.in_(spec.types))
    if spec.within is not None:
        where.append(
            exists().where(
                EntityAncestor.entity_id == Entity.id,
                EntityAncestor.ancestor_id == spec.within,
                EntityAncestor.depth > 0,
            )
        )
    where.append(_not_excluded(spec, tree, Entity.id))

    # Field filters always apply to the listed entity: they belong to its type.
    where.extend(_field_condition(f, fields) for f in spec.fields)
    own = _positive(spec, tree, Entity.id)
    if spec.aggregate_up and own:
        # Tags and text may instead be matched by a (non-excluded) descendant.
        descendant = EntityAncestor.entity_id
        via_descendant = exists().where(
            EntityAncestor.ancestor_id == Entity.id,
            EntityAncestor.depth > 0,
            *_positive(spec, tree, descendant),
            _not_excluded(spec, tree, descendant),
        )
        where.append(or_(and_(*own), via_descendant))
    else:
        where.extend(own)
    return and_(true(), *where)


def _positive(spec: SearchSpec, tree: TagTree, entity_id: IdColumn) -> list[ColumnElement[bool]]:
    """Include groups and text, for the entity in ``entity_id``."""
    conditions = [
        _has_any_tag(entity_id, _subtree(tree, tag), spec.inherit_tags) for tag in spec.include
    ]
    if spec.text:
        conditions.append(text_condition(spec.text, entity_id))
    return conditions


def _not_excluded(spec: SearchSpec, tree: TagTree, entity_id: IdColumn) -> ColumnElement[bool]:
    excluded = frozenset().union(*(_subtree(tree, t) for t in spec.exclude))
    if not excluded:
        return true()
    return ~_has_any_tag(entity_id, excluded, spec.inherit_tags)


def _subtree(tree: TagTree, tag_id: int) -> frozenset[int]:
    """A tag's subtree; a tag no longer in the tree (e.g. from a saved search) is just itself,
    so including it matches nothing and excluding it excludes nothing."""
    return tree.descendants(tag_id) if tag_id in tree else frozenset({tag_id})


def _has_any_tag(
    entity_id: IdColumn, tag_ids: frozenset[int], inherit: bool
) -> ColumnElement[bool]:
    """Whether the entity carries any of ``tag_ids``: directly, or with ``inherit`` also via
    any ancestor (the closure's depth-0 self row covers its own tags, §8 "Query shape")."""
    if not inherit:
        return exists().where(EntityTag.entity_id == entity_id, EntityTag.tag_id.in_(tag_ids))
    lineage = aliased(EntityAncestor)
    return exists().where(
        lineage.entity_id == entity_id,
        EntityTag.entity_id == lineage.ancestor_id,
        EntityTag.tag_id.in_(tag_ids),
    )


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
