"""What detail pages show (#86): sections from the theme's DetailView, or the default."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, insert

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.detail import (
    DetailSection,
    EntityDetail,
    breadcrumbs,
    default_detail_view,
    detail_view_for,
    load_detail,
)
from tagalot.core.ingest import IngestSession
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.models import EntityContains, Resource, ResourceKind, ResourceStatus, Root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import DetailView, Section, Theme, contains, field, related, role
from tagalot.themes.api import Entity as ThemeEntity


class Actor(ThemeEntity):
    born: int | None = field("Born")
    secret: str | None = field("Secret", detail=False)
    roles = [role("photo", kinds={"image"})]


class Collection(ThemeEntity):
    roles = [role("folder", kinds={"dir"}, primary=True)]


class Movie(ThemeEntity):
    year: int | None = field("Year")
    roles = [
        role("video", kinds={"video"}, primary=True),
        role("poster", kinds={"image"}, thumbnail=True),
        role("still", kinds={"image"}, many=True, label="Screenshots"),
    ]


class MoviesTheme(Theme):
    id, name = "movies", "Movies"
    entities = [Actor, Collection, Movie]
    containment = [contains(Collection, Movie)]
    relationships = [related("cast", Actor, Movie, label="Films", reverse_label="Cast")]
    views = [
        DetailView(
            Movie,
            [
                Section.fields(),
                Section.role("poster"),
                Section.gallery("still"),
                Section.related("cast"),
                Section.custom(lambda entity: None),
            ],
        ),
        DetailView(Actor, [Section.fields(), Section.related("cast")]),
    ]


@dataclass
class Env:
    reader: Engine
    schema: ThemeSchema
    ids: dict[str, int]

    def detail(self, title: str) -> EntityDetail:
        with self.reader.connect() as conn:
            found = load_detail(
                conn, self.schema, self.ids[title], lambda r: "/media" if r == "r" else None
            )
        assert found is not None
        return found


def _section(detail: EntityDetail, kind: str) -> DetailSection:
    return next(s for s in detail.sections if s.kind == kind)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "M", ThemeRef("movies", 1)))
    schema = open_theme(engine, keep, MoviesTheme).schema
    ids: dict[str, int] = {}

    def fill(conn: Connection) -> None:
        conn.execute(insert(Root).values(id="r", name="Media share"))
        conn.execute(insert(Root).values(id="gone", name="Old drive"))
        files = [
            ("r", "Noir/Heat.mkv", ResourceKind.FILE, 9000, ResourceStatus.OK),
            ("r", "Noir/Heat poster.jpg", ResourceKind.FILE, 100, ResourceStatus.MISSING),
            ("r", "Noir/stills/b.jpg", ResourceKind.FILE, 50, ResourceStatus.OK),
            ("gone", "stills/a.jpg", ResourceKind.FILE, 40, ResourceStatus.OFFLINE),
            ("r", "Noir", ResourceKind.DIR, None, ResourceStatus.OK),
        ]
        for root, relpath, kind, size, status in files:
            ids[relpath] = conn.execute(
                insert(Resource)
                .values(root_id=root, relpath=relpath, kind=kind, size=size, status=status)
                .returning(Resource.id)
            ).scalar_one()
        ctx = IngestSession(conn, schema)
        heat = ctx.upsert(Movie, "heat", title="Heat", year=1995)
        pacino = ctx.upsert(Actor, "pacino", title="Al Pacino", born=1940, secret="x")
        de_niro = ctx.upsert(Actor, "de niro", title="Robert De Niro")
        noir = ctx.upsert(Collection, "noir", title="Noir")
        ctx.link(heat, ids["Noir/Heat.mkv"], "video")
        ctx.link(heat, ids["Noir/Heat poster.jpg"], "poster")
        ctx.link(heat, ids["Noir/stills/b.jpg"], "still", sort_order=2)
        ctx.link(heat, ids["stills/a.jpg"], "still", sort_order=1)
        ctx.link(noir, ids["Noir"], "folder")
        ctx.relate("cast", pacino, heat)
        ctx.relate("cast", de_niro, heat)
        ctx.contain(noir, heat)
        ctx.flush()
        ids.update(Heat=heat.id, **{"Al Pacino": pacino.id, "Noir": noir.id})

    with DbWriter(engine) as writer:
        writer.run(fill)
    reader = create_keep_engine(keep.db_path, read_only=True)
    yield Env(reader, schema, ids)
    reader.dispose()


def test_a_declared_view_gives_its_sections_in_order(env: Env) -> None:
    heat = env.detail("Heat")
    assert (heat.title, heat.type_label) == ("Heat", "Movie")
    assert [(s.kind, s.title) for s in heat.sections] == [
        ("fields", "Details"),
        ("role", "Poster"),
        ("gallery", "Screenshots"),
        ("related", "Cast"),
        ("custom", ""),
    ]


def test_fields_skip_those_hidden_from_details(env: Env) -> None:
    fields = _section(env.detail("Al Pacino"), "fields").fields
    assert [(f.label, f.value) for f in fields] == [("Born", 1940)]


def test_role_files_with_their_root_status_and_path(env: Env) -> None:
    heat = env.detail("Heat")
    [poster] = _section(heat, "role").files
    assert (poster.root_name, poster.relpath, poster.status) == (
        "Media share",
        "Noir/Heat poster.jpg",
        ResourceStatus.MISSING,
    )
    assert poster.path is not None
    assert poster.path.endswith("Heat poster.jpg")
    stills = _section(heat, "gallery").files
    assert [s.relpath for s in stills] == ["stills/a.jpg", "Noir/stills/b.jpg"]  # sort order
    assert stills[0].path is None  # its root isn't configured on this machine


def test_related_from_either_side(env: Env) -> None:
    cast = _section(env.detail("Heat"), "related")
    assert [e.title for e in cast.entities] == ["Al Pacino", "Robert De Niro"]
    films = _section(env.detail("Al Pacino"), "related")
    assert (films.title, [e.title for e in films.entities]) == ("Films", ["Heat"])


def test_the_default_view_for_a_container(env: Env) -> None:
    noir = env.detail("Noir")
    assert [(s.kind, s.title) for s in noir.sections] == [
        ("fields", "Details"),
        ("role", "Folder"),
        ("contents", "Contents"),
    ]
    assert _section(noir, "contents").count == 1
    assert [f.relpath for f in _section(noir, "role").files] == ["Noir"]


def test_default_views() -> None:
    kinds = [s.kind for s in default_detail_view(Movie, is_container=False).sections]
    assert kinds == ["fields", "role"]
    assert [s.name for s in default_detail_view(Movie, False).sections] == [None, "video"]
    assert [s.kind for s in default_detail_view(Actor, True).sections] == ["fields", "contents"]


def test_the_theme_view_wins(env: Env) -> None:
    assert detail_view_for(env.schema, Movie).sections[1] == Section.role("poster")


def test_a_deleted_entity_has_no_page(env: Env) -> None:
    with env.reader.connect() as conn:
        assert load_detail(conn, env.schema, 999_999, lambda r: None) is None


# --- breadcrumbs (#88) ---


class Folder(ThemeEntity):
    pass


class FoldersTheme(Theme):
    id, name = "folders", "Folders"
    entities = [Folder]
    containment = [contains(Folder, Folder)]


@pytest.fixture
def folders(tmp_path: Path) -> Iterator[tuple[Engine, DbWriter, ThemeSchema, dict[str, int]]]:
    keep, engine = open_keep_database(create_keep(tmp_path / "f", "F", ThemeRef("folders", 1)))
    schema = open_theme(engine, keep, FoldersTheme).schema
    ids: dict[str, int] = {}

    def fill(conn: Connection) -> None:
        ctx = IngestSession(conn, schema)
        refs = {n: ctx.upsert(Folder, n, title=n) for n in ["Art", "Pixel", "Sprites", "Zoo"]}
        ctx.contain(refs["Art"], refs["Pixel"])
        ctx.contain(refs["Pixel"], refs["Sprites"])
        ctx.contain(refs["Zoo"], refs["Sprites"])  # a second container
        ctx.flush()
        ids.update({n: r.id for n, r in refs.items()})

    with DbWriter(engine) as writer:
        writer.run(fill)
        reader = create_keep_engine(keep.db_path, read_only=True)
        yield reader, writer, schema, ids
        reader.dispose()


def _crumbs(reader: Engine, entity_id: int) -> tuple[list[str], int]:
    with reader.connect() as conn:
        chain, others = breadcrumbs(conn, entity_id)
    return [c.title for c in chain], others


def test_breadcrumbs_follow_the_first_container_by_title(
    folders: tuple[Engine, DbWriter, ThemeSchema, dict[str, int]],
) -> None:
    reader, _, schema, ids = folders
    assert _crumbs(reader, ids["Sprites"]) == (["Art", "Pixel"], 1)  # also in Zoo
    assert _crumbs(reader, ids["Pixel"]) == (["Art"], 0)
    assert _crumbs(reader, ids["Art"]) == ([], 0)
    with reader.connect() as conn:
        detail = load_detail(conn, schema, ids["Sprites"], lambda r: None)
    assert detail is not None
    assert [c.title for c in detail.breadcrumbs] == ["Art", "Pixel"]
    assert detail.other_parents == 1


def test_breadcrumbs_stop_at_a_cycle(
    folders: tuple[Engine, DbWriter, ThemeSchema, dict[str, int]],
) -> None:
    reader, writer, _, ids = folders
    # The closure maintenance refuses cycles; write one straight into the table (bad data).
    writer.run(
        lambda conn: conn.execute(
            insert(EntityContains).values(parent_id=ids["Sprites"], child_id=ids["Art"])
        )
    )
    chain, _ = _crumbs(reader, ids["Pixel"])  # Pixel < Art < Sprites < Pixel …
    assert chain == ["Sprites", "Art"]  # each container once, then it stops
