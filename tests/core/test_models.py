"""Tests for the core schema: tables, constraints, delete behavior, and column types."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, exc, func, insert, inspect, select
from sqlalchemy.orm import Session

from tagalot.core.db import create_keep_engine
from tagalot.core.models import (
    Base,
    Entity,
    EntityAncestor,
    EntityContains,
    EntityResource,
    EntityTag,
    FieldProvenance,
    FieldSource,
    Resource,
    ResourceKind,
    ResourceStatus,
    Root,
    SavedSearch,
    SchemaVersion,
    Tag,
    TagAlias,
)

CORE_TABLES = {
    "root",
    "resource",
    "entity",
    "entity_resource",
    "entity_contains",
    "entity_ancestor",
    "tag",
    "tag_alias",
    "entity_tag",
    "entity_keyword",
    "keyword_ignored",
    "file_tag_removal",
    "field_provenance",
    "saved_search",
    "triage_dismissal",
    "entity_merge",
    "entity_merge_resource",
    "dedupe_dismissal",
    "user_relation",
    "user_order",
    "user_contains",
    "online_response",
    "entity_note",
    "note_key_removal",
    "schema_version",
}
FTS_TABLES = {
    "entity_fts",
    # FTS5's shadow tables
    "entity_fts_config",
    "entity_fts_content",
    "entity_fts_data",
    "entity_fts_docsize",
    "entity_fts_idx",
}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def _entity(session: Session, title: str = "E", **kw: object) -> Entity:
    e = Entity(type=kw.pop("type", "generic.file"), title=title, **kw)
    session.add(e)
    session.flush()
    return e


def _resource(session: Session, relpath: str = "a.txt", root_id: str = "r1") -> Resource:
    if session.get(Root, root_id) is None:
        session.add(Root(id=root_id, name=root_id))
        session.flush()  # no ORM relationship, so SQLAlchemy won't order this insert for us
    r = Resource(root_id=root_id, relpath=relpath, kind=ResourceKind.FILE, ext=".txt")
    session.add(r)
    session.flush()
    return r


def _count(session: Session, model: type[Base]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_creates_every_core_table(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) == CORE_TABLES | FTS_TABLES


def test_documented_indexes_exist(engine: Engine) -> None:
    insp = inspect(engine)

    def columns(table: str) -> set[tuple[str | None, ...]]:
        return {tuple(ix["column_names"]) for ix in insp.get_indexes(table)}

    assert ("root_id", "relpath") in columns("resource")
    assert ("fingerprint",) in columns("resource")
    assert ("type", "ingest_key") in columns("entity")
    assert ("resource_id",) in columns("entity_resource")
    assert ("ancestor_id", "entity_id") in columns("entity_ancestor")
    assert ("tag_id", "entity_id") in columns("entity_tag")
    # SQLAlchemy can't reflect expression indexes; read SQLite's catalog instead.
    with engine.connect() as conn:
        sql = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'uq_tag_sibling_name'"
        ).scalar()
    assert sql is not None
    assert "UNIQUE" in sql
    assert "coalesce(parent_id, 0)" in sql.lower()
    assert "lower(name)" in sql.lower()


# --- tag sibling-name uniqueness (DESIGN.md §5, issue #17) ---


@pytest.mark.parametrize(
    ("first", "second", "allowed"),
    [
        ((None, "Rock"), (None, "Rock"), False),  # duplicate at the root level (NULL parent)
        ((None, "Rock"), (None, "ROCK"), False),  # case variant at the root level
        (("Genre", "Rock"), ("Genre", "rock"), False),  # case variant under a parent
        (("Genre", "Rock"), ("Mood", "Rock"), True),  # same name, different parents
        ((None, "Rock"), ("Genre", "Rock"), True),  # root level vs. under a parent
        ((None, "Rock"), (None, "Rock Classics"), True),
        # SQLite's lower() folds ASCII only; tags.py compares with casefold() (DESIGN.md §5).
        ((None, "Ärger"), (None, "ärger"), True),
    ],
)
def test_tag_sibling_names_are_unique(
    session: Session,
    first: tuple[str | None, str],
    second: tuple[str | None, str],
    allowed: bool,
) -> None:
    parents = {name: Tag(name=name) for name in ("Genre", "Mood")}
    session.add_all(parents.values())
    session.flush()

    def add(parent: str | None, name: str) -> None:
        session.add(Tag(parent_id=parents[parent].id if parent else None, name=name))
        session.flush()

    add(*first)
    if allowed:
        add(*second)
    else:
        with pytest.raises(exc.IntegrityError, match="UNIQUE"):
            add(*second)


def test_renaming_into_a_sibling_clash_is_rejected(session: Session) -> None:
    a, b = Tag(name="Jazz"), Tag(name="Blues")
    session.add_all([a, b])
    session.flush()
    b.name = "jazz"
    with pytest.raises(exc.IntegrityError, match="UNIQUE"):
        session.flush()


# --- entity identity ---


def test_ingest_key_is_unique_per_type(session: Session) -> None:
    _entity(session, type="music.artist", ingest_key="the beatles")
    _entity(session, type="music.album", ingest_key="the beatles")  # other type: fine
    _entity(session, type="music.artist")  # NULL keys never collide
    _entity(session, type="music.artist")
    with pytest.raises(exc.IntegrityError, match="UNIQUE"):
        _entity(session, type="music.artist", ingest_key="the beatles")


def test_relpath_is_unique_per_root(session: Session) -> None:
    _resource(session, "Album/01.flac", "r1")
    _resource(session, "Album/01.flac", "r2")
    with pytest.raises(exc.IntegrityError, match="UNIQUE"):
        _resource(session, "Album/01.flac", "r1")


# --- delete behavior ---


def test_deleting_an_entity_removes_its_link_rows(session: Session) -> None:
    album, song = _entity(session, "Album"), _entity(session, "Song")
    tag = Tag(name="Jazz")
    session.add(tag)
    session.flush()
    session.add_all(
        [
            EntityResource(entity_id=song.id, resource_id=_resource(session).id, role="audio"),
            EntityContains(parent_id=album.id, child_id=song.id),
            EntityAncestor(entity_id=song.id, ancestor_id=song.id, depth=0),
            EntityAncestor(entity_id=song.id, ancestor_id=album.id, depth=1),
            EntityTag(entity_id=song.id, tag_id=tag.id),
            FieldProvenance(entity_id=song.id, field="title", source=FieldSource.USER),
        ]
    )
    session.commit()

    session.delete(song)
    session.commit()
    for model in (EntityResource, EntityContains, EntityAncestor, EntityTag, FieldProvenance):
        assert _count(session, model) == 0, model.__name__
    assert _count(session, Resource) == 1  # files are never deleted with entities
    assert _count(session, Tag) == 1


def test_deleting_a_resource_unlinks_it_and_clears_thumbnails(session: Session) -> None:
    resource = _resource(session)
    entity = _entity(session, thumb_resource_id=resource.id)
    session.add(EntityResource(entity_id=entity.id, resource_id=resource.id, role="cover"))
    session.commit()

    session.delete(resource)
    session.commit()
    session.refresh(entity)
    assert entity.thumb_resource_id is None
    assert _count(session, EntityResource) == 0


def test_deleting_a_tag_removes_its_uses_and_aliases(session: Session) -> None:
    tag = Tag(name="Jazz")
    session.add(tag)
    session.flush()
    session.add_all(
        [
            EntityTag(entity_id=_entity(session).id, tag_id=tag.id),
            TagAlias(tag_id=tag.id, alias="jz"),
        ]
    )
    session.commit()
    session.delete(tag)
    session.commit()
    assert _count(session, EntityTag) == 0
    assert _count(session, TagAlias) == 0


def test_deleting_a_tag_with_children_is_refused(session: Session) -> None:
    parent = Tag(name="Genre")
    session.add(parent)
    session.flush()
    session.add(Tag(parent_id=parent.id, name="Jazz"))
    session.commit()
    session.delete(parent)
    with pytest.raises(exc.IntegrityError, match="FOREIGN KEY"):
        session.commit()


def test_deleting_a_root_with_resources_is_refused(session: Session) -> None:
    _resource(session)
    session.commit()
    session.delete(session.get_one(Root, "r1"))
    with pytest.raises(exc.IntegrityError, match="FOREIGN KEY"):
        session.commit()


# --- column types ---


def test_enum_columns_reject_unknown_values(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(insert(Root).values(id="r1", name="R"))
    with pytest.raises(exc.IntegrityError, match="CHECK"), engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO resource (root_id, relpath, kind, ext, status, first_seen_at, "
            "last_seen_at) VALUES ('r1', 'x', 'file', '', 'deleted', '2026-01-01', '2026-01-01')"
        )


def test_resource_defaults_and_enums_round_trip(session: Session) -> None:
    r = _resource(session)
    session.commit()
    session.expire_all()
    r = session.get_one(Resource, r.id)
    assert r.kind is ResourceKind.FILE
    assert r.status is ResourceStatus.OK
    assert r.first_seen_at.tzinfo is UTC


def test_datetimes_are_stored_in_utc_and_returned_aware(session: Session) -> None:
    pacific = timezone(timedelta(hours=-8))
    seen = datetime(2026, 9, 26, 10, 30, tzinfo=pacific)
    r = _resource(session)
    r.last_seen_at = seen
    session.commit()
    session.expire_all()
    loaded = session.get_one(Resource, r.id).last_seen_at
    assert loaded == seen
    assert loaded.tzinfo is UTC
    assert loaded.hour == 18


def test_naive_datetimes_are_rejected(session: Session) -> None:
    r = _resource(session)
    r.last_seen_at = datetime(2026, 9, 26)
    with pytest.raises(exc.StatementError, match="naive datetime"):
        session.flush()


def test_json_and_binary_columns_round_trip(session: Session) -> None:
    e = _entity(session, extra={"notes": "café ♪", "rating": 4, "tags": ["a", "b"]})
    r = _resource(session)
    r.fingerprint = bytes(range(32))
    r.mtime_ns = 1_790_000_000_123_456_789
    session.add(SavedSearch(name="Jazz", definition={"include": [1, 2], "text": None}))
    session.add(SchemaVersion(component="core", version=1))
    session.commit()
    session.expire_all()
    assert session.get_one(Entity, e.id).extra == {
        "notes": "café ♪",
        "rating": 4,
        "tags": ["a", "b"],
    }
    loaded = session.get_one(Resource, r.id)
    assert loaded.fingerprint == bytes(range(32))
    assert loaded.mtime_ns == 1_790_000_000_123_456_789
    assert session.scalar(select(SavedSearch.definition)) == {"include": [1, 2], "text": None}


def test_updated_at_changes_on_update(session: Session) -> None:
    e = _entity(session)
    session.commit()
    first = e.updated_at
    e.title = "Renamed"
    session.commit()
    assert e.updated_at >= first
    assert e.created_at <= e.updated_at
