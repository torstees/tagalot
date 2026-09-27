"""Keeping the ``entity_fts`` text-search table in sync (DESIGN.md §5, §8).

The DB writer maintains ``entity_fts``; there are no triggers. Every code path that creates,
renames, re-extracts, edits ``extra`` of, merges, or deletes entities calls
:func:`sync_entities` for them in the same transaction, so an entity and its index row always
commit together. :func:`rebuild_search_index` repairs the whole table.
"""

import logging
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any

from sqlalchemy import Connection, delete, func, insert, select

from tagalot.core.models import Entity, entity_fts

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

TextSource = Callable[[Connection, Sequence[int]], Mapping[int, Sequence[str | None]]]
"""Returns each entity's ``search="text"`` field values; themes supply it (M4)."""


def no_text_fields(conn: Connection, ids: Sequence[int]) -> Mapping[int, Sequence[str | None]]:
    """The default :data:`TextSource` until themes exist: no extra text."""
    return {}


def fts_document(title: str, extra: Any, texts: Iterable[str | None] = ()) -> tuple[str, str]:
    """The ``(title, body)`` indexed for an entity.

    ``body`` is the text-search field values followed by the ``extra`` values, flattened
    (nested lists and objects contribute their values, not their keys; numbers become text;
    booleans and nulls are skipped), joined by spaces.
    """
    parts = [t for t in texts if t]
    parts.extend(_flatten(extra))
    return title, " ".join(p for p in parts if p.strip())


def sync_entities(
    conn: Connection, entity_ids: Iterable[int], text_source: TextSource = no_text_fields
) -> None:
    """Refresh the index rows of these entities; rows of entities that no longer exist go."""
    ids = sorted(set(entity_ids))
    for batch in _chunks(ids):
        conn.execute(delete(entity_fts).where(entity_fts.c.rowid.in_(batch)))
        _insert_documents(conn, batch, text_source)


def rebuild_search_index(conn: Connection, text_source: TextSource = no_text_fields) -> int:
    """Rebuild ``entity_fts`` from scratch ("Rebuild search index"). Returns the row count."""
    conn.execute(delete(entity_fts))
    ids = list(conn.scalars(select(Entity.id).order_by(Entity.id)))
    for batch in _chunks(ids):
        _insert_documents(conn, batch, text_source)
    logger.info("Rebuilt the search index (%d entities)", len(ids))
    return len(ids)


def index_is_consistent(conn: Connection) -> bool:
    """Every entity has exactly one index row with its current title (a cheap health check)."""
    entities = conn.scalar(select(func.count()).select_from(Entity)) or 0
    rows = conn.scalar(select(func.count()).select_from(entity_fts)) or 0
    matching = (
        conn.scalar(
            select(func.count())
            .select_from(Entity)
            .join(entity_fts, entity_fts.c.rowid == Entity.id)
            .where(entity_fts.c.title == Entity.title)
        )
        or 0
    )
    return entities == rows == matching


def _insert_documents(conn: Connection, ids: Sequence[int], text_source: TextSource) -> None:
    rows = conn.execute(select(Entity.id, Entity.title, Entity.extra).where(Entity.id.in_(ids)))
    texts = text_source(conn, ids)
    documents = []
    for entity_id, title, extra in rows:
        doc_title, body = fts_document(title, extra, texts.get(entity_id, ()))
        documents.append({"rowid": entity_id, "title": doc_title, "body": body})
    if documents:
        conn.execute(insert(entity_fts), documents)


def _flatten(value: Any) -> Iterator[str]:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, int | float):
        yield str(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _flatten(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _flatten(item)


def _chunks[T](items: Sequence[T], size: int = BATCH_SIZE) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
