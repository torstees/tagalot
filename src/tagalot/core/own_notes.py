"""The keep's own fields in notes and Markdown front matter (#584, DESIGN.md §9 *Notes*).

A front-matter key named like one of the item's type's own fields (its name or label, as
notes match keys: case, spaces, ``_`` and ``-`` aside) fills that field rather than
becoming an Extra field, marked as the note's (``note``) or, from a Markdown file's own
front matter, the file's (``extracted``). A value the user edited is never replaced
(rule 9), and the file's wins over the note's, as for a theme's fields. A value that
doesn't read as the field's kind stays an Extra field, and the scan says so.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import Connection, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core.extra_keys import EXTRA, as_text, convert, loose_key, read_again
from tagalot.core.models import Entity, FieldProvenance, FieldSource, OwnFieldRead, utcnow
from tagalot.core.own_fields import OWN, OwnField, OwnFields, rating_problem
from tagalot.core.own_values import raw_values, store_value


def applied_own_fields(conn: Connection) -> dict[str, list[OwnField]]:
    """The keep's own fields its notes and Markdown files are read with, by type, as
    the last scan remembered them (:func:`sync_own_fields`)."""
    found: dict[str, list[OwnField]] = {}
    rows = conn.execute(
        select(OwnFieldRead.type_id, OwnFieldRead.name, OwnFieldRead.label, OwnFieldRead.kind)
    )
    for type_id, name, label, kind in rows:
        found.setdefault(type_id, []).append(OwnField(type_id, name, label, kind))
    return found


def sync_own_fields(conn: Connection, fields: OwnFields) -> bool:
    """Remember ``fields`` as the ones notes and Markdown files are read with; when
    their names, labels, or kinds differ from the last ones, mark every note and
    Markdown file to be read again (a key now a field's fills it). Returns whether they
    differed."""
    wanted = {
        (f.type_id, f.name, f.label, f.kind)
        for by_name in fields.values()
        for f in by_name.values()
    }
    rows = conn.execute(
        select(OwnFieldRead.type_id, OwnFieldRead.name, OwnFieldRead.label, OwnFieldRead.kind)
    )
    held = {(t, n, lb, k) for t, n, lb, k in rows}
    if held == wanted:
        return False
    read_again(conn)
    conn.execute(delete(OwnFieldRead))
    if wanted:
        conn.execute(
            sqlite_insert(OwnFieldRead),
            [{"type_id": t, "name": n, "label": lb, "kind": k} for t, n, lb, k in sorted(wanted)],
        )
    return True


def own_by_key(fields: Iterable[OwnField]) -> dict[str, OwnField]:
    """Each own field by its name and its label, as :func:`loose_key` makes them."""
    found: dict[str, OwnField] = {}
    for own in fields:
        found.setdefault(loose_key(own.name), own)
        found.setdefault(loose_key(own.label), own)
    return found


def read_own(own: OwnField, value: Any) -> Any:
    """A front-matter value as the field's: ``ValueError`` when it isn't one."""
    found = convert(value, own.python_type)
    if own.kind == "rating" and (problem := rating_problem(found)) is not None:
        raise ValueError(problem)
    return found


def take_own(
    values: Mapping[str, Any], fields: Iterable[OwnField]
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Split front-matter ``values``: those of own fields (by field name), the rest, and
    what didn't read as its field's kind (those stay in the rest, as Extra fields)."""
    by_key = own_by_key(fields)
    mine: dict[str, Any] = {}
    rest: dict[str, Any] = {}
    problems: list[str] = []
    for key, value in values.items():
        own = by_key.get(loose_key(str(key)))
        if own is None or value is None or value == "" or value == []:
            rest[key] = value
            continue
        try:
            mine[own.name] = read_own(own, value)
        except ValueError as e:
            problems.append(f"{key}: {e}, so it stays an Extra field")
            if as_text(value) is not None:
                rest[key] = value
    return mine, rest, problems


def absorb_extras(
    conn: Connection, entity_id: int, given: Mapping[str, Any], fields: Iterable[OwnField]
) -> bool:
    """Extra fields named like the own fields a note or file now ``given`` (by field name),
    which a note or file gave before (found in review: ``pages: soon``, edited on the page,
    stayed beside the field the file's ``pages`` now fills). Each goes; one the user edited
    moves into its field first, unless the field has a value of the user's or it isn't of
    the field's kind (then it stays). An Extra field the user typed (no provenance) stays.
    Returns whether anything changed."""
    by_key = {k: f for k, f in own_by_key(fields).items() if f.name in given}
    if not by_key:
        return False
    extra = conn.scalar(select(Entity.extra).where(Entity.id == entity_id))
    if not extra:
        return False
    sources = dict(
        conn.execute(
            select(FieldProvenance.field, FieldProvenance.source).where(
                FieldProvenance.entity_id == entity_id
            )
        ).all()
    )
    after = dict(extra)
    for key, text in extra.items():
        own = by_key.get(loose_key(str(key)))
        source = sources.get(EXTRA + key)
        if own is None or source is None:
            continue
        if source == FieldSource.USER and sources.get(own.key) != FieldSource.USER:
            try:
                value = read_own(own, text)
            except ValueError:
                continue
            store_value(conn, entity_id, own.name, value)
            _set_source(conn, entity_id, own.key, FieldSource.USER)
            sources[own.key] = FieldSource.USER
        del after[key]
        conn.execute(
            delete(FieldProvenance).where(
                FieldProvenance.entity_id == entity_id, FieldProvenance.field == EXTRA + key
            )
        )
    if after == extra:
        return False
    conn.execute(update(Entity).where(Entity.id == entity_id).values(extra=after))
    return True


def apply_own(
    conn: Connection, entity_id: int, given: Mapping[str, Any], source: FieldSource
) -> tuple[bool, list[str]]:
    """Give an item the own fields' values a note (``source`` ``note``) or its file
    (``extracted``) gives, by field name, and take away those it gave before and doesn't
    now. The user's values stay; a note gives way to the file. Returns whether a value
    changed, and the fields taken away (the note may give them: read it again)."""
    sources: dict[str, FieldSource] = {
        name.removeprefix(OWN): src
        for name, src in conn.execute(
            select(FieldProvenance.field, FieldProvenance.source).where(
                FieldProvenance.entity_id == entity_id,
                FieldProvenance.field.startswith(OWN, autoescape=True),
            )
        ).all()
    }
    current = raw_values(conn, [entity_id]).get(entity_id, {})
    changed = False
    for name, value in given.items():
        held = sources.get(name)
        if held == FieldSource.USER or (
            source == FieldSource.NOTE and held == FieldSource.EXTRACTED
        ):
            continue
        if current.get(name) != (int(value) if isinstance(value, bool) else value):
            store_value(conn, entity_id, name, value)
            changed = True
        if held != source:
            _set_source(conn, entity_id, OWN + name, source)
    dropped = [name for name, held in sources.items() if held == source and name not in given]
    for name in dropped:
        store_value(conn, entity_id, name, None)
        changed = True
    if dropped:
        conn.execute(
            delete(FieldProvenance).where(
                FieldProvenance.entity_id == entity_id,
                FieldProvenance.field.in_([OWN + n for n in dropped]),
            )
        )
    return changed, dropped


def _set_source(conn: Connection, entity_id: int, name: str, source: FieldSource) -> None:
    upsert = sqlite_insert(FieldProvenance).values(
        entity_id=entity_id, field=name, source=source, updated_at=utcnow()
    )
    conn.execute(
        upsert.on_conflict_do_update(
            index_elements=["entity_id", "field"],
            set_={"source": upsert.excluded.source, "updated_at": upsert.excluded.updated_at},
        )
    )
