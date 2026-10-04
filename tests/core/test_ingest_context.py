"""Tests for the ingest context (DESIGN.md §9 "Ingest context")."""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, insert, select, update

from tagalot.core import closure
from tagalot.core.db import open_keep_database
from tagalot.core.ingest import IngestError, IngestSession
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.models import (
    Entity,
    EntityAncestor,
    EntityResource,
    FieldProvenance,
    FieldSource,
    Resource,
    ResourceKind,
    ResourceStatus,
    Root,
    entity_fts,
)
from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.tags import TagTree
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import (
    EntityRef,
    IngestContext,
    ResourceInfo,
    Theme,
    contains,
    field,
    related,
    role,
)


class Artist(ThemeEntity):
    country: str | None = field("Country", search="choice")
    bio: str | None = field("Biography", search="text")


class Album(ThemeEntity):
    year: int | None = field("Year", search="range")
    released: date | None = field("Released")
    roles = [
        role("folder", kinds={"dir"}, primary=True),
        role("cover", kinds={"image"}, thumbnail=True),
        role("scan", kinds={"image"}, many=True),
    ]


class Label(ThemeEntity):
    pass


class Music(Theme):
    id, name = "music", "Music"
    entities = [Artist, Album, Label]
    containment = [contains(Artist, Album)]
    relationships = [related("released_by", Album, Label, many=False)]


def _typecheck(session: IngestSession) -> IngestContext:
    return session  # mypy: IngestSession satisfies the public protocol


@dataclass
class Env:
    engine: Engine
    schema: ThemeSchema
    resources: list[ResourceInfo]

    def session(self, conn: Connection) -> IngestSession:
        return IngestSession(conn, self.schema)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("music", 1)))
    schema = open_theme(engine, keep, Music).schema
    resources = []
    with engine.begin() as conn:
        conn.execute(insert(Root).values(id="r", name="R"))
        for i, rel in enumerate(["Album", "Album/cover.jpg", "Album/back.jpg", "Album/scan1.jpg"]):
            kind = ResourceKind.DIR if rel == "Album" else ResourceKind.FILE
            rid = conn.execute(
                insert(Resource).values(root_id="r", relpath=rel, kind=kind).returning(Resource.id)
            ).scalar_one()
            resources.append(ResourceInfo(rid, "r", rel, kind.value, "", None, None, f"/x/{rel}"))
            del i
    yield Env(engine, schema, resources)
    engine.dispose()


# --- upsert ---


def test_upsert_creates_everything_an_entity_needs(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        ref = ctx.upsert(Artist, "björk", title="Björk", country="Iceland", bio="Singer")
        ctx.flush()
        assert ref.type == "music.artist"
        assert conn.execute(select(Entity.title, Entity.ingest_key)).one() == ("Björk", "björk")
        assert conn.scalar(select(env.schema.entities[Artist].table.c.country)) == "Iceland"
        assert conn.execute(select(EntityAncestor.entity_id, EntityAncestor.depth)).all() == [
            (ref.id, 0)
        ]
        provenance = dict(conn.execute(select(FieldProvenance.field, FieldProvenance.source)).all())
        assert provenance == {
            "title": FieldSource.EXTRACTED,
            "country": FieldSource.EXTRACTED,
            "bio": FieldSource.EXTRACTED,
        }
        assert conn.execute(select(entity_fts.c.title, entity_fts.c.body)).one() == (
            "Björk",
            "Singer",  # text-search fields reach the index
        )


def test_title_defaults_to_the_key(env: Env) -> None:
    with env.engine.begin() as conn:
        ref = env.session(conn).upsert(Label, "one little indian")
        assert conn.scalar(select(Entity.title).where(Entity.id == ref.id)) == "one little indian"


def test_upsert_again_finds_and_updates(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        first = ctx.upsert(Album, "homogenic", title="Homogenic", year=1996)
        again = ctx.upsert(Album, "homogenic", title="Homogenic (Remaster)", year=1997)
        assert first == again
        assert ctx.get(again).title == "Homogenic (Remaster)"
        assert ctx.get(again).fields["year"] == 1997


def test_user_edits_are_never_overwritten(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        ref = ctx.upsert(Album, "homogenic", title="Homogenic", year=1997)
        # The user corrects the title and the year (M11 does this through the UI).
        conn.execute(update(Entity).where(Entity.id == ref.id).values(title="Homogénic"))
        conn.execute(
            update(env.schema.entities[Album].table)
            .where(env.schema.entities[Album].table.c.id == ref.id)
            .values(year=1996)
        )
        conn.execute(
            update(FieldProvenance)
            .where(
                FieldProvenance.entity_id == ref.id, FieldProvenance.field.in_(["title", "year"])
            )
            .values(source=FieldSource.USER)
        )
        # A rescan extracts again, and also finds a release date.
        again = ctx.upsert(
            Album, "homogenic", title="Homogenic", year=1997, released=date(1997, 9, 22)
        )
        record = ctx.get(again)
        assert again == ref  # still found by its key, despite the renamed title
        assert record.title == "Homogénic"
        assert record.fields["year"] == 1996
        assert record.fields["released"] == date(1997, 9, 22)
        sources = dict(conn.execute(select(FieldProvenance.field, FieldProvenance.source)).all())
        assert sources["year"] is FieldSource.USER
        assert sources["released"] is FieldSource.EXTRACTED


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda ctx: ctx.upsert(Album, "x", tempo=120), "Album has no field tempo"),
        (lambda ctx: ctx.upsert(Album, ""), "ingest key can't be empty"),
        (lambda ctx: ctx.upsert(ThemeEntity, "x"), "is not an entity of the 'music' theme"),
        (lambda ctx: ctx.find(Album, tempo=1), "has no field 'tempo'"),
        (lambda ctx: ctx.get(EntityRef(1, "movies.movie")), "not a type of this theme"),
    ],
)
def test_theme_mistakes_are_ingest_errors(env: Env, call: object, message: str) -> None:
    with env.engine.begin() as conn, pytest.raises(IngestError, match=message):
        call(env.session(conn))  # type: ignore[operator]


# --- find and get ---


def test_find(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        a = ctx.upsert(Album, "a", title="Debut", year=1993)
        b = ctx.upsert(Album, "b", title="Post", year=1995)
        c = ctx.upsert(Album, "c", title="Telegram", year=1995)
        assert ctx.find(Album, year=1995) == [b, c]
        assert ctx.find(Album, title="Debut") == [a]
        assert ctx.find(Album, ingest_key="c") == [c]
        assert ctx.find(Album) == [a, b, c]
        assert ctx.find(Artist) == []


def test_get_returns_extra_too(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        ref = ctx.upsert(Artist, "k", title="K")
        conn.execute(update(Entity).where(Entity.id == ref.id).values(extra={"notes": "x"}))
        record = ctx.get(ref)
        assert (record.title, record.fields, record.extra) == (
            "K",
            {"country": None, "bio": None},
            {"notes": "x"},
        )


# --- links ---


def _links(conn: Connection) -> set[tuple[str, int, int]]:
    rows = conn.execute(
        select(EntityResource.role, EntityResource.resource_id, EntityResource.sort_order)
    )
    return {(r, i, s) for r, i, s in rows}


def test_links(env: Env) -> None:
    folder, cover, back, scan = env.resources
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        album = ctx.upsert(Album, "homogenic")
        ctx.link(album, folder, "folder")
        ctx.link(album, cover, "cover")
        ctx.link(album, back.id, "cover")  # single-valued: replaces the first cover
        ctx.link(album, scan, "scan", sort_order=2)
        ctx.link(album, cover, "scan", sort_order=1)  # the same file in another role is fine
        ctx.link(album, scan, "scan", sort_order=5)  # relinking updates the order
        assert _links(conn) == {
            ("folder", folder.id, 0),
            ("cover", back.id, 0),
            ("scan", scan.id, 5),
            ("scan", cover.id, 1),
        }
        ctx.unlink(album, cover, "scan")
        assert ("scan", cover.id, 1) not in _links(conn)
        with pytest.raises(IngestError, match="Album has no role 'poster'"):
            ctx.link(album, cover, "poster")


# --- containment ---


def test_containment_is_applied_at_flush(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        artist = ctx.upsert(Artist, "björk")
        album = ctx.upsert(Album, "homogenic")
        ctx.contain(artist, album)
        assert (
            conn.scalar(
                select(EntityAncestor.depth).where(
                    EntityAncestor.ancestor_id == artist.id, EntityAncestor.entity_id == album.id
                )
            )
            is None
        )
        report = ctx.flush()
        assert report.edges_added == 1
        assert closure.verify(conn).ok
        ctx.uncontain(artist, album)
        assert ctx.flush().edges_removed == 1


def test_the_last_contain_or_uncontain_in_a_batch_wins(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        artist, album = ctx.upsert(Artist, "a"), ctx.upsert(Album, "b")
        ctx.contain(artist, album)
        ctx.uncontain(artist, album)
        assert ctx.contents(artist) == []
        assert ctx.flush().edges_added == 0
        ctx.uncontain(artist, album)
        ctx.contain(artist, album)
        ctx.contain(artist, album)
        assert ctx.contents(artist) == [album]
        assert ctx.flush().edges_added == 1
        ctx.contain(artist, album)  # already there
        ctx.uncontain(artist, album)
        assert ctx.flush().edges_removed == 1


def test_undeclared_containment(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        artist, album = ctx.upsert(Artist, "a"), ctx.upsert(Album, "b")
        with pytest.raises(
            IngestError, match=r"doesn't declare that music\.album contains music\.artist"
        ):
            ctx.contain(album, artist)


# --- relationships ---


def test_relationships(env: Env) -> None:
    table = env.schema.relationships["released_by"].table
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        album = ctx.upsert(Album, "homogenic")
        indian, elektra = ctx.upsert(Label, "one little indian"), ctx.upsert(Label, "elektra")
        ctx.relate("released_by", album, indian)
        ctx.relate("released_by", album, indian)  # idempotent
        assert conn.execute(select(table.c.a_id, table.c.b_id)).all() == [(album.id, indian.id)]
        ctx.unrelate("released_by", album, indian)
        assert conn.execute(select(table.c.a_id)).all() == []
        with pytest.raises(
            IngestError, match=r"links Album to Label, not music\.label to music\.album"
        ):
            ctx.relate("released_by", indian, album)
        with pytest.raises(IngestError, match="no relationship 'cast'"):
            ctx.relate("cast", album, elektra)


def test_many_false_relationship_replaces_the_partner(env: Env) -> None:
    table = env.schema.relationships["released_by"].table
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        a1, a2 = ctx.upsert(Album, "a1"), ctx.upsert(Album, "a2")
        label = ctx.upsert(Label, "l")
        ctx.relate("released_by", a1, label)
        ctx.relate("released_by", a2, label)  # each label has at most one album here
        assert conn.execute(select(table.c.a_id, table.c.b_id)).all() == [(a2.id, label.id)]


def test_related_reads_either_side(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        album = ctx.upsert(Album, "homogenic")
        label = ctx.upsert(Label, "one little indian")
        artist = ctx.upsert(Artist, "bjork")
        assert ctx.related("released_by", album) == []
        ctx.relate("released_by", album, label)
        assert ctx.related("released_by", album) == [label]
        assert ctx.related("released_by", label) == [album]
        with pytest.raises(IngestError, match="isn't part of the 'released_by'"):
            ctx.related("released_by", artist)
        with pytest.raises(IngestError, match="no relationship 'cast'"):
            ctx.related("cast", album)


# --- reporting and search ---


def test_warnings_are_collected(env: Env) -> None:
    folder = env.resources[0]
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        ctx.warn(folder, "no cover image found")
        ctx.warn(None, "general problem")
        report = ctx.flush()
    assert [(w.relpath, w.message) for w in report.warnings] == [
        ("Album", "no cover image found"),
        (None, "general problem"),
    ]


def test_flushed_entities_are_searchable(env: Env) -> None:
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        ctx.upsert(Artist, "björk", title="Björk", bio="Icelandic singer")
        ctx.upsert(Album, "homogenic", title="Homogenic")
        ctx.flush()
    with env.engine.connect() as conn:
        tree = TagTree([], {})
        assert [h.title for h in run_search(conn, SearchSpec(text="icelandic"), tree)] == ["Björk"]
        assert [h.title for h in run_search(conn, SearchSpec(text="homog"), tree)] == ["Homogenic"]


# --- update and entities_of ---


def test_update_a_known_entity_respects_user_edits(env: Env) -> None:
    with env.engine.begin() as conn:
        ref = env.session(conn).upsert(Album, "k", title="Old", year=1990)
    with env.engine.begin() as conn:  # the user edits the year (its own write)
        conn.execute(
            update(FieldProvenance)
            .where(FieldProvenance.entity_id == ref.id, FieldProvenance.field == "year")
            .values(source=FieldSource.USER)
        )
    with env.engine.begin() as conn:  # a later scan
        ctx = env.session(conn)
        ctx.update(ref, title="New", year=2000)
        record = ctx.get(ref)
        assert (record.title, record.fields["year"]) == ("New", 1990)
        with pytest.raises(IngestError, match="Album has no field tempo"):
            ctx.update(ref, tempo=1)
        with pytest.raises(IngestError, match="no longer exists"):
            ctx.update(EntityRef(999, "music.album"), title="x")


def test_entities_of_a_resource(env: Env) -> None:
    folder, cover, *_ = env.resources
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        album = ctx.upsert(Album, "a")
        other = ctx.upsert(Album, "b")
        ctx.link(album, folder, "folder")
        ctx.link(album, cover, "cover")
        ctx.link(other, cover, "scan")
        assert ctx.entities_of(folder) == [album]
        assert ctx.entities_of(cover.id) == [album, other]
        assert ctx.entities_of(cover, "scan") == [other]
        assert ctx.entities_of(folder, "cover") == []


# --- resource_at (#297) ---


def test_resource_at_finds_a_file_in_the_same_root(env: Env) -> None:
    album, cover = env.resources[0], env.resources[1]
    beside = ResourceInfo(album.id, "r", "Album", "dir", "", None, None, str(Path("/x/Album")))
    with env.engine.begin() as conn:
        ctx = env.session(conn)
        found = ctx.resource_at(beside, "Album/cover.jpg")
        assert found is not None
        assert (found.id, found.relpath, found.kind) == (cover.id, "Album/cover.jpg", "file")
        assert found.path == str(Path("/x/Album/cover.jpg"))  # this machine's path
        assert ctx.resource_at(beside, "album/COVER.jpg") == found  # case, when nothing else
        assert ctx.resource_at(beside, "./Album/../Album/cover.jpg") == found
        assert ctx.resource_at(beside, "Album/none.jpg") is None
        assert ctx.resource_at(beside, "../Album/cover.jpg") is None  # outside the root
        conn.execute(
            update(Resource).where(Resource.id == cover.id).values(status=ResourceStatus.MISSING)
        )
        assert ctx.resource_at(beside, "Album/cover.jpg") is None  # known to be missing


def test_resource_at_with_no_path_here(env: Env) -> None:
    # A root with no path on this computer: the resource is found, with no path either.
    beside = ResourceInfo(env.resources[2].id, "r", "Album/back.jpg", "file", ".jpg", 1, 1, "")
    with env.engine.begin() as conn:
        found = env.session(conn).resource_at(beside, "Album/cover.jpg")
    assert found is not None
    assert found.path == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows paths")
def test_resource_at_on_a_unc_share(env: Env) -> None:
    beside = ResourceInfo(
        env.resources[2].id, "r", "Album/back.jpg", "file", ".jpg", 1, 1,
        r"\\nas\books\Album\back.jpg",
    )  # fmt: skip
    with env.engine.begin() as conn:
        found = env.session(conn).resource_at(beside, "Album/cover.jpg")
    assert found is not None
    assert found.path == r"\\nas\books\Album\cover.jpg"
