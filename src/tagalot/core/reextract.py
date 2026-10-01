"""Reading chosen items' details from their files again (DESIGN.md §5, §6).

:func:`reextract` marks the files linked to some entities pending and ingests them at
once, as a scan would, so the theme reads them again. Fields the user edited keep their
values, unless ``replace_edits`` asks for the files' values: then the user's provenance on
those entities is cleared first. Either way the result is one undo step: a
:class:`ReextractChange` holds each entity's title, fields, and provenance before and after,
and :func:`restore_entities` puts either back.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, delete, insert, select, update

from tagalot.core import fts
from tagalot.core.ingest import theme_text_source
from tagalot.core.keep import RootConfig
from tagalot.core.models import (
    Entity,
    EntityResource,
    FieldProvenance,
    FieldSource,
    Resource,
    ResourceStatus,
)
from tagalot.core.scanjob import ingest_pending
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import Theme


@dataclass(frozen=True)
class EntityState:
    """One entity's title, theme field values, and field provenance."""

    title: str
    values: dict[str, Any]
    provenance: dict[str, FieldSource]


@dataclass(frozen=True)
class ReextractChange:
    """Everything needed to undo and redo a re-read."""

    label: str
    before: dict[int, EntityState]
    after: dict[int, EntityState]


@dataclass
class ReextractReport:
    change: ReextractChange
    read: int = 0
    """Files read again."""
    offline: int = 0
    """Items whose files couldn't be reached (their root is offline, or the files are gone)."""
    errors: list[tuple[str, str]] = field(default_factory=list)


def reextract(
    writer: DbWriter,
    reader: Engine,
    schema: ThemeSchema,
    theme: type[Theme],
    roots: Sequence[RootConfig],
    root_path: Callable[[str], str],
    entity_ids: Iterable[int],
    *,
    replace_edits: bool = False,
    theme_options: dict[str, Any] | None = None,
    when: datetime | None = None,
) -> ReextractReport:
    """Read the files of ``entity_ids`` again, now. Runs in a worker (it reads files)."""
    ids = sorted(set(entity_ids))
    with reader.connect() as conn:
        before = snapshot(conn, schema, ids)
        files = conn.execute(
            select(Resource.id, Resource.root_id, Resource.status, EntityResource.entity_id)
            .join(EntityResource, EntityResource.resource_id == Resource.id)
            .where(EntityResource.entity_id.in_(ids), EntityResource.by_user.is_(False))
        ).all()
    readable = {r.id: r.root_id for r in files if r.status == ResourceStatus.OK}
    reachable = {r.entity_id for r in files if r.status == ResourceStatus.OK}

    def mark(conn: Connection) -> None:
        if replace_edits:
            conn.execute(
                delete(FieldProvenance).where(
                    FieldProvenance.entity_id.in_(reachable),
                    FieldProvenance.source == FieldSource.USER,
                )
            )
        if readable:
            conn.execute(update(Resource).where(Resource.id.in_(readable)).values(ingested_at=None))

    writer.run(mark)
    report = ReextractReport(ReextractChange("", {}, {}))
    report.read = len(readable)
    report.offline = len([i for i in ids if i not in reachable])
    by_id = {r.id: r for r in roots}
    for root_id in sorted(set(readable.values())):
        root = by_id.get(root_id)
        if root is None:
            continue
        result = ingest_pending(
            writer,
            reader,
            root,
            root_path(root_id),
            theme=theme,
            schema=schema,
            theme_options=theme_options,
            when=when,
        )
        report.errors.extend(result.ingest_errors)
    with reader.connect() as conn:
        after = snapshot(conn, schema, ids)
    items = "1 item" if len(ids) == 1 else f"{len(ids):,} items"
    label = f"Re-read {items} from their files" + (
        ", replacing your edits" if replace_edits else ""
    )
    report.change = ReextractChange(label, before, after)
    return report


def snapshot(conn: Connection, schema: ThemeSchema, ids: Sequence[int]) -> dict[int, EntityState]:
    """The title, field values, and provenance of each entity (for undo)."""
    states: dict[int, EntityState] = {}
    if not ids:
        return states
    rows = conn.execute(select(Entity.id, Entity.type, Entity.title).where(Entity.id.in_(ids)))
    provenance: dict[int, dict[str, FieldSource]] = {}
    for entity_id, name, source in conn.execute(
        select(FieldProvenance.entity_id, FieldProvenance.field, FieldProvenance.source).where(
            FieldProvenance.entity_id.in_(ids)
        )
    ):
        provenance.setdefault(entity_id, {})[name] = source
    for entity_id, type_id, title in rows:
        try:
            table = schema.by_type_id(type_id).table
        except KeyError:
            values: dict[str, Any] = {}
        else:
            row = conn.execute(select(table).where(table.c.id == entity_id)).first()
            values = {k: v for k, v in row._mapping.items() if k != "id"} if row else {}
        states[entity_id] = EntityState(title, values, provenance.get(entity_id, {}))
    return states


def restore_entities(
    conn: Connection, schema: ThemeSchema, change: ReextractChange, *, forward: bool
) -> None:
    """Put the entities as they were before the re-read (undo) or after it (redo)."""
    states = change.after if forward else change.before
    existing = set(conn.scalars(select(Entity.id).where(Entity.id.in_(states))))
    for entity_id, state in states.items():
        if entity_id not in existing:
            continue
        conn.execute(update(Entity).where(Entity.id == entity_id).values(title=state.title))
        type_id = conn.scalar(select(Entity.type).where(Entity.id == entity_id))
        if state.values and type_id is not None:
            table = schema.by_type_id(type_id).table
            conn.execute(update(table).where(table.c.id == entity_id).values(**state.values))
        conn.execute(delete(FieldProvenance).where(FieldProvenance.entity_id == entity_id))
        if state.provenance:
            conn.execute(
                insert(FieldProvenance),
                [
                    {"entity_id": entity_id, "field": name, "source": source}
                    for name, source in state.provenance.items()
                ],
            )
    fts.sync_entities(conn, existing, theme_text_source(schema))
