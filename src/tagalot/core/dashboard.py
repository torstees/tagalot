"""What the dashboard shows, read in one go (DESIGN.md §12 "Dashboard").

:func:`load_dashboard` gathers, in one read transaction (call it from a worker): items per
type, how many are untagged (Triage's list), the most recently added items, each root's
status, and the most and least used tags; and the theme's own cards (:func:`theme_cards`):
statistics and most common values it declares, computed here in SQL, and cards its methods
compute through a read-only :class:`DashboardReader`.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Connection, func, select

from tagalot.core.formats import format_value
from tagalot.core.ingest import IngestSession
from tagalot.core.models import Entity, EntityTag
from tagalot.core.root_admin import RootStatus, root_statuses
from tagalot.core.search import SearchHit, count_matches
from tagalot.core.search_spec import (
    ChoiceFilter,
    FieldFilter,
    RangeFilter,
    SearchSpec,
    TextFilter,
    TextMatch,
)
from tagalot.core.tags import TagTree
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.triage import UNTAGGED
from tagalot.themes.api import (
    DashboardCard,
    EntityRef,
    Record,
    StatCard,
    TopValuesCard,
    entity_fields,
    entity_plural,
)
from tagalot.themes.api import Entity as ThemeEntity

logger = logging.getLogger(__name__)

RECENT = 12
"""Recently added items shown."""
TAGS_SHOWN = 5
"""Tags in each of the most and least used lists."""


@dataclass(frozen=True)
class TypeCount:
    type_id: str
    plural: str
    count: int


@dataclass(frozen=True)
class TagUse:
    tag_id: int
    path: str
    """The tag's full path, ``Places / Iceland``."""
    count: int
    """Items it is applied to directly."""


@dataclass(frozen=True)
class Dashboard:
    types: tuple[TypeCount, ...]
    """Every type of the theme, in its order, with how many items it has."""
    total: int
    untagged: int
    recent: tuple[SearchHit, ...]
    roots: dict[str, RootStatus]
    most_used: tuple[TagUse, ...]
    least_used: tuple[TagUse, ...]
    """Tags used least (unused ones first): candidates for a tidy-up."""
    theme_cards: tuple["ThemeCard", ...] = ()

    @property
    def untagged_share(self) -> float:
        return self.untagged / self.total if self.total else 0.0


def load_dashboard(conn: Connection, schema: ThemeSchema, tree: TagTree) -> Dashboard:
    """Everything the dashboard shows; see the module docstring."""
    counts = dict(conn.execute(select(Entity.type, func.count()).group_by(Entity.type)).all())
    types = tuple(
        TypeCount(t.type_id, entity_plural(t.entity), int(counts.get(t.type_id, 0)))
        for t in schema.entities.values()
    )
    recent = tuple(
        SearchHit(i, t, title)
        for i, t, title in conn.execute(
            select(Entity.id, Entity.type, Entity.title)
            .order_by(Entity.created_at.desc(), Entity.id.desc())
            .limit(RECENT)
        )
    )
    uses = dict(
        conn.execute(select(EntityTag.tag_id, func.count()).group_by(EntityTag.tag_id)).all()
    )
    tag_uses = [TagUse(t, " / ".join(tree.path(t)), int(uses.get(t, 0))) for t in tree]
    by_most = sorted(tag_uses, key=lambda u: (-u.count, u.path.casefold()))
    by_least = sorted(tag_uses, key=lambda u: (u.count, u.path.casefold()))
    return Dashboard(
        types=types,
        total=sum(c.count for c in types),
        untagged=count_matches(conn, SearchSpec(triage=UNTAGGED), tree),
        recent=recent,
        roots=root_statuses(conn),
        most_used=tuple(u for u in by_most[:TAGS_SHOWN] if u.count),
        least_used=_least(by_least, by_most[:TAGS_SHOWN]),
        theme_cards=theme_cards(conn, schema),
    )


def _least(by_least: Sequence[TagUse], most: Sequence[TagUse]) -> tuple[TagUse, ...]:
    """The least used tags, not repeating ones already listed as most used."""
    shown = {u.tag_id for u in most if u.count}
    return tuple(u for u in by_least if u.tag_id not in shown)[:TAGS_SHOWN]


# --- the theme's cards ---


@dataclass(frozen=True)
class CardRow:
    label: str
    value: str = ""
    search: tuple[str, FieldFilter] | None = None
    """``(type id, filter)``: a search of those items (a top value of a searchable field)."""


@dataclass(frozen=True)
class ThemeCard:
    title: str
    rows: tuple[CardRow, ...]
    description: str = ""
    error: str | None = None
    """Why the card couldn't be computed (a theme bug); the dashboard shows it."""


def theme_cards(conn: Connection, schema: ThemeSchema) -> tuple[ThemeCard, ...]:
    """The theme's declared cards, then those its methods compute."""
    theme = schema.theme
    cards = [_declared(conn, schema, card) for card in theme.dashboard]
    methods = theme.card_methods()
    if methods:
        instance = theme()
        reader = DashboardReader(conn, schema)
        for name, spec in methods.items():
            try:
                rows = _rows(getattr(instance, name)(reader))
            except Exception as e:
                logger.exception("Dashboard card %r failed", spec.title)
                cards.append(
                    ThemeCard(spec.title, (), spec.description, f"{type(e).__name__}: {e}")
                )
            else:
                cards.append(ThemeCard(spec.title, rows, spec.description))
    return tuple(cards)


def _declared(conn: Connection, schema: ThemeSchema, card: DashboardCard) -> ThemeCard:
    try:
        if isinstance(card, StatCard):
            value = stat_value(conn, schema, card.type, card.field, card.how)
            shown = (
                "\u2014"
                if value is None
                else _shown(value, _display(card.type, card.field), card.how)
            )
            return ThemeCard(card.title, (CardRow(shown),), card.description)
        return ThemeCard(card.title, _top_values(conn, schema, card), card.description)
    except Exception as e:
        logger.exception("Dashboard card %r failed", card.title)
        return ThemeCard(card.title, (), card.description, f"{type(e).__name__}: {e}")


def stat_value(
    conn: Connection, schema: ThemeSchema, type: type[ThemeEntity], field: str, how: str
) -> float | None:
    """A sum, average, minimum, maximum, or count (of values) of a field over a type."""
    table = schema.entities[type].table
    column = table.c[field]
    expression: ColumnElement[Any]
    if how == "sum":
        expression = func.sum(column)
    elif how == "avg":
        expression = func.avg(column)
    elif how == "min":
        expression = func.min(column)
    elif how == "max":
        expression = func.max(column)
    elif how == "count":
        expression = func.count(column)
    else:
        raise ValueError(f"no statistic {how!r}")
    value = conn.scalar(select(expression).select_from(table))
    if value is None:
        return None
    return int(value) if how == "count" else value


def _top_values(conn: Connection, schema: ThemeSchema, card: TopValuesCard) -> tuple[CardRow, ...]:
    table = schema.entities[card.type].table
    column = table.c[card.field]
    rows = conn.execute(
        select(column, func.count().label("n"))
        .where(column.is_not(None))
        .group_by(column)
        .order_by(func.count().desc(), column)
        .limit(card.limit)
    )
    type_id = schema.theme.type_id_of(card.type)
    info = next(f for f in entity_fields(card.type) if f.name == card.field)
    found = []
    for value, n in rows:
        search = _value_filter(card.field, info.spec.search, value)
        label = format_value(value, info.spec.display) or str(value)
        found.append(CardRow(label, f"{n:,}", (type_id, search) if search else None))
    return tuple(found)


def _value_filter(field: str, search: str | None, value: Any) -> FieldFilter | None:
    """A filter for items with exactly this value, as the field is searched; ``None``
    when it isn't searchable (the value is then shown without a link)."""
    if search == "choice":
        return ChoiceFilter(field, (value,))
    if search == "range":
        return RangeFilter(field, value, value)
    if search == "text" and isinstance(value, str):
        return TextFilter(field, value, TextMatch.STARTS_WITH)
    return None


def _display(type: type[ThemeEntity], field: str) -> str | None:
    info = next((f for f in entity_fields(type) if f.name == field), None)
    return info.spec.display if info is not None else None


def _shown(value: float, display: str | None, how: str) -> str:
    """A statistic for people: in the field's display format, else as a number."""
    if how != "count":
        formatted = format_value(value, display)
        if formatted is not None:
            return formatted
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.1f}"


def _rows(result: object) -> tuple[CardRow, ...]:
    if isinstance(result, str):
        return (CardRow(result),)
    if not isinstance(result, Sequence):
        raise TypeError(f"a dashboard card returns rows or a text, not {result!r}")
    rows = []
    for row in result:
        if not (isinstance(row, tuple) and len(row) == 2):
            raise TypeError(f"a dashboard card's row is (label, value), not {row!r}")
        rows.append(CardRow(str(row[0]), str(row[1])))
    return tuple(rows)


class DashboardReader:
    """``ctx`` for :func:`~tagalot.themes.api.dashboard_card` methods: read-only."""

    def __init__(self, conn: Connection, schema: ThemeSchema) -> None:
        self._conn = conn
        self._schema = schema
        self._ingest = IngestSession(conn, schema)  # for its read-only lookups

    def count(self, type: type[ThemeEntity], **equals: Any) -> int:
        table = self._schema.entities[type].table
        query = select(func.count()).select_from(table)
        for name, value in equals.items():
            if name not in table.c:
                raise ValueError(f"{type.__name__} has no field {name!r}")
            query = query.where(table.c[name] == value)
        return int(self._conn.scalar(query) or 0)

    def find(self, type: type[ThemeEntity], **equals: Any) -> list[EntityRef]:
        return self._ingest.find(type, **equals)

    def get(self, entity: EntityRef) -> Record:
        return self._ingest.get(entity)

    def stat(self, type: type[ThemeEntity], field: str, how: str = "sum") -> float | None:
        return stat_value(self._conn, self._schema, type, field, how)
