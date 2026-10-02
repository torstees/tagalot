"""What the dashboard shows, read in one go (DESIGN.md §12 "Dashboard").

:func:`load_dashboard` gathers, in one read transaction (call it from a worker): items per
type, how many are untagged (Triage's list), the most recently added items, each root's
status, and the most and least used tags.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, func, select

from tagalot.core.models import Entity, EntityTag
from tagalot.core.root_admin import RootStatus, root_statuses
from tagalot.core.search import SearchHit, count_matches
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tags import TagTree
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.triage import UNTAGGED
from tagalot.themes.api import entity_plural

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
    )


def _least(by_least: Sequence[TagUse], most: Sequence[TagUse]) -> tuple[TagUse, ...]:
    """The least used tags, not repeating ones already listed as most used."""
    shown = {u.tag_id for u in most if u.count}
    return tuple(u for u in by_least if u.tag_id not in shown)[:TAGS_SHOWN]
