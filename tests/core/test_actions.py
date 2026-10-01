"""Theme actions: running them, their outputs, and undoing what they changed (#106)."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, insert, select

from tagalot.builtin_themes.music import Album, Artist, MusicTheme, Song, artist_ref
from tagalot.core import closure
from tagalot.core.actions import (
    ActionChange,
    ActionError,
    ActionResult,
    actions_for,
    restore_action,
    run_action,
)
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import (
    Entity,
    EntityContains,
    EntityResource,
    EntityTag,
    FieldProvenance,
    entity_fts,
)
from tagalot.core.scanjob import scan_root
from tagalot.core.tags import add_tag
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema, build_theme_schema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import ActionContext, EntityRef, action
from tests.themes.test_music import T0, library

__all__ = ["library"]  # fixture


class MusicWithActions(MusicTheme):
    """The music theme, with actions to exercise every kind of write."""

    @action("Add the year", [Album])
    def add_year(self, albums: Sequence[EntityRef], ctx: ActionContext) -> None:
        for album in albums:
            record = ctx.get(album)
            ctx.update(album, title=f"{record.title} ({record.fields['year']})")
        ctx.message(f"Added the year to {len(albums)} albums.")

    @action("Make a playlist", ["folder"])  # by role: albums have a folder
    def playlist(self, albums: Sequence[EntityRef], ctx: ActionContext) -> None:
        lines = [r.path for a in albums for s in ctx.contents(a) for r in ctx.resources(s)[:1]]
        path = ctx.temp_path("../album.m3u")  # folders are dropped
        Path(path).write_text("\n".join(lines), encoding="utf-8")
        ctx.open(path)
        ctx.reveal(path)

    @action("Merge songs", [Song])
    def merge(self, songs: Sequence[EntityRef], ctx: ActionContext) -> None:
        """Move every file onto the first song and delete the others."""
        first, *rest = songs
        for song in rest:
            for resource in ctx.linked(song):
                ctx.link(first, resource, "audio", sort_order=1)
            ctx.delete(song)

    @action("New artist", [Album])
    def new_artist(self, albums: Sequence[EntityRef], ctx: ActionContext) -> None:
        guest = artist_ref("Guest Star", ctx)
        for album in albums:
            ctx.contain(guest, album)

    @action("Explode", [Album])
    def explode(self, albums: Sequence[EntityRef], ctx: ActionContext) -> None:
        ctx.update(albums[0], title="half done")
        raise RuntimeError("boom")


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path
    temp: Path

    def run(self, method: str, *titles: str) -> ActionResult:
        ids = [self.entity(t) for t in titles]
        return self.writer.run(
            lambda conn: run_action(
                conn,
                self.schema,
                MusicWithActions(),
                method,
                ids,
                root_path=lambda _: str(self.files),
                temp_dir=lambda: self.temp,
            )
        )

    def restore(self, change: ActionChange, *, forward: bool) -> None:
        self.writer.run(lambda conn: restore_action(conn, self.schema, change, forward=forward))

    def entity(self, title: str) -> int:
        with self.reader.connect() as conn:
            found = conn.scalars(select(Entity.id).where(Entity.title == title)).first()
        assert found is not None, title
        return found

    def titles(self) -> list[str]:
        with self.reader.connect() as conn:
            return sorted(conn.scalars(select(Entity.title)))

    def state(self) -> tuple[object, ...]:
        """Everything an undo must put back, derived tables included."""
        with self.reader.connect() as conn:
            # Not updated_at (an undo is an update) or the thumbnail memo (chosen afresh).
            entities = select(
                Entity.id, Entity.type, Entity.title, Entity.ingest_key, Entity.extra,
                Entity.created_at,
            )  # fmt: skip
            rows = tuple(
                sorted(tuple(r) for r in conn.execute(query).all())
                for query in (
                    entities,
                    select(EntityContains),
                    select(EntityTag),
                    select(EntityResource),
                    select(
                        FieldProvenance.entity_id, FieldProvenance.field, FieldProvenance.source
                    ),
                    *(select(t.table) for t in self.schema.entities.values()),
                )
            )
            search = sorted(tuple(r) for r in conn.execute(select(entity_fts.c.rowid)).all())
        assert self.writer.run(lambda conn: closure.verify(conn).ok)
        return (*rows, search)


@pytest.fixture
def env(tmp_path: Path, library: Path) -> Iterator[Env]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "M", ThemeRef("music", 1)))
    schema = open_theme(engine, keep, MusicWithActions).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        root = RootConfig("r", "Music", str(library))
        scan_root(
            writer, reader, root, root.path, when=T0, theme=MusicWithActions, schema=schema
        )  # fmt: skip
        yield Env(writer, reader, schema, library, tmp_path / "temp")
    reader.dispose()


def test_actions_for_a_type() -> None:
    schema_theme = MusicWithActions
    labels = {
        entity.__name__: [a.label for a in actions_for(build_theme_schema(schema_theme), t)]
        for entity, t in ((e, schema_theme.type_id_of(e)) for e in (Artist, Album, Song))
    }
    assert labels == {
        "Artist": [],
        "Album": ["Add the year", "Make a playlist", "New artist", "Explode"],  # by type or role
        "Song": ["Merge songs"],
    }


def test_an_action_edits_and_is_undone_and_redone(env: Env) -> None:
    before = env.state()
    result = env.run("add_year", "Kind of Blue")
    assert result.changed
    assert result.outputs == [("message", "Added the year to 1 albums.")]
    assert "Kind of Blue (1959)" in env.titles()
    after = env.state()
    env.restore(result.change, forward=False)
    assert env.state() == before
    env.restore(result.change, forward=True)
    assert env.state() == after


def test_outputs_and_contents_in_order(env: Env) -> None:
    result = env.run("playlist", "Kind of Blue")
    assert not result.changed  # it only wrote a file of its own
    playlist = env.temp / "album.m3u"  # "../" was dropped
    assert result.outputs == [("open", str(playlist)), ("reveal", str(playlist))]
    lines = playlist.read_text(encoding="utf-8").splitlines()
    assert [Path(p).name for p in lines] == ["01 So What.flac", "02 Freddie Freeloader.mp3"]


def test_undoing_a_delete_brings_back_the_entity_its_tags_and_edges(env: Env) -> None:
    so_what, freddie = env.entity("So What"), env.entity("Freddie Freeloader")
    tag = env.writer.run(lambda conn: add_tag(conn, None, "Favorites"))
    env.writer.run(
        lambda conn: conn.execute(insert(EntityTag).values(entity_id=freddie, tag_id=tag))
    )
    before = env.state()
    result = env.run("merge", "So What", "Freddie Freeloader")
    assert "Freddie Freeloader" not in env.titles()
    with env.reader.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(EntityTag)) == 0
    after = env.state()
    env.restore(result.change, forward=False)
    assert env.state() == before  # same id, links, album, tag, search row
    assert env.entity("Freddie Freeloader") == freddie
    env.restore(result.change, forward=True)
    assert env.state() == after
    assert so_what == env.entity("So What")


def test_undoing_a_creation_deletes_it(env: Env) -> None:
    before = env.state()
    result = env.run("new_artist", "Kind of Blue", "The Box")
    assert "Guest Star" in env.titles()
    env.restore(result.change, forward=False)
    assert "Guest Star" not in env.titles()
    assert env.state() == before


def test_a_failing_action_changes_nothing(env: Env) -> None:
    before = env.state()
    with pytest.raises(ActionError, match="Explode failed: boom"):
        env.run("explode", "Kind of Blue")
    assert env.state() == before


def test_an_action_only_gets_the_items_it_applies_to(env: Env) -> None:
    result = env.run("add_year", "Kind of Blue", "So What")  # a song is ignored
    assert result.outputs == [("message", "Added the year to 1 albums.")]
    with pytest.raises(ActionError, match="doesn't apply"):
        env.run("add_year", "So What")
    with pytest.raises(ActionError, match="no action 'nope'"):
        env.run("nope", "So What")
