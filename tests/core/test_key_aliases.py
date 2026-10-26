"""The user's own front-matter key aliases (#411): a key a theme doesn't read fills a field
(``teaser`` -> Country) in notes and in Markdown files' own front matter, instead of
becoming an Extra field; kept per keep and per user, re-read when they change, and written
back under the key the file uses."""

import datetime
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.extra_keys import aliased_fields, applied_aliases
from tagalot.core.keep import (
    KeepConfig,
    RootConfig,
    ThemeRef,
    create_keep,
    load_keep_config,
    save_keep_config,
)
from tagalot.core.keep_settings import with_note_key_aliases
from tagalot.core.models import FieldSource
from tagalot.core.note_keys import (
    NoteKeyError,
    check_alias,
    key_aliases,
    sync_aliases,
    with_alias,
)
from tagalot.core.notes import NoteText, map_note
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.settings import Settings, load_settings, save_settings
from tagalot.core.theme_db import open_theme
from tagalot.core.writeback import aliased_keys
from tagalot.core.writer import DbWriter
from tagalot.themes.api import IngestContext, entity_fields, read_front_matter
from tests.core.test_notes import Env, Studio, Studios

INFO = "Aurora (2001)/info.md"
NOTE = "Aurora (2001)/Aurora (2001).md"
FIELDS = entity_fields(Studio)


def _map(front: dict[str, Any], aliases: dict[str, str], ignored: tuple[str, ...] = ()) -> Any:
    return map_note(NoteText(front, ""), FIELDS, ignored, aliases)


MAPPED = {
    "by the field's name": ({"nation": "Iceland"}, {"nation": "country"}, {"country": "Iceland"}),
    "by its label": ({"Nation": "Iceland"}, {"nation": "Country"}, {"country": "Iceland"}),
    "converted": ({"since": "1999"}, {"since": "Year founded"}, {"founded": 1999}),
    "no alias: an Extra field": ({"nation": "Iceland"}, {}, {}),
    "an unknown field: an Extra field": ({"nation": "Iceland"}, {"nation": "planet"}, {}),
}


@pytest.mark.parametrize("case", MAPPED.values(), ids=MAPPED.keys())
def test_a_notes_aliased_key_fills_its_field(
    case: tuple[dict[str, Any], dict[str, str], dict[str, Any]],
) -> None:
    front, aliases, fields = case
    found = _map(front, aliases)
    assert found.fields == fields
    assert set(found.extra) == set(front) - {k for k in front if fields}


def test_the_fields_own_key_wins() -> None:
    """Either order: the alias then stays an Extra field."""
    for front in (
        {"nation": "Iceland", "country": "Norway"},
        {"country": "Norway", "nation": "Iceland"},
    ):
        found = _map(front, {"nation": "country"})
        assert found.fields == {"country": "Norway"}
        assert found.extra == {"nation": "Iceland"}


def test_an_alias_that_doesnt_convert_stays_an_extra_field() -> None:
    found = _map({"since": "long ago"}, {"since": "founded"})
    assert found.fields == {}
    assert found.extra == {"since": "long ago"}
    assert found.problems == ["since: isn't a whole number"]


def test_what_an_alias_cant_take_over() -> None:
    """A field's own name or label, and keys Tagalot reads itself, are never aliased; an
    ignored key stays ignored."""
    aliases = {"country": "founded", "title": "country", "cover": "country", "nation": "country"}
    assert set(aliased_fields(FIELDS, aliases)) == {"nation"}
    found = _map({"nation": "Iceland"}, {"nation": "country"}, ignored=("nation",))
    assert (found.fields, found.extra) == ({}, {})


def test_checking_and_merging_aliases() -> None:
    assert check_alias(" synopsis ", " Description ") == ("synopsis", "Description")
    for key, field_name, message in (
        ("", "x", "Type the key"),
        ("blurb", " ", "Name the field"),
        ("tagalot-type", "x", "Tagalot's own"),
        ("Tags", "x", "reads 'Tags' itself"),
    ):
        with pytest.raises(NoteKeyError, match=message):
            check_alias(key, field_name)
    mine, keeps = {"Synopsis": "description", "tags": "x"}, {"synopsis": "summary", "lede": "y"}
    assert key_aliases(mine, keeps) == {"synopsis": "summary", "lede": "y"}  # the keep's wins
    assert with_alias({"A": "x", "b": "y"}, "a", "z") == {"A": "z", "b": "y"}
    assert with_alias({"A": "x"}, "c", "z") == {"A": "x", "c": "z"}
    assert with_alias({"A": "x", "b": "y"}, "a", None) == {"b": "y"}


def test_aliases_in_keep_toml_and_settings_toml(tmp_path: Path) -> None:
    keep = create_keep(tmp_path / "k", "K", ThemeRef("generic", 1))
    config = with_note_key_aliases(keep.config, {"synopsis": "Description", "the key": "x"})
    save_keep_config(config, keep.toml_path)
    text = keep.toml_path.read_text(encoding="utf-8")
    assert "[notes.key_aliases]\nsynopsis = 'Description'\n'the key' = 'x'" in text
    loaded: KeepConfig = load_keep_config(keep.toml_path)
    assert loaded.note_key_aliases == {"synopsis": "Description", "the key": "x"}

    path = tmp_path / "settings.toml"
    save_settings(Settings(note_key_aliases={"lede": "Plot"}), path)
    assert load_settings(path).note_key_aliases == {"lede": "Plot"}


class InfoStudios(Studios):
    """Studios whose ``info.md`` front matter is read: its ``country`` (written every time,
    as themes do) and its other keys as Extra fields."""

    id = "aliasstudios"
    extensions = frozenset({".png", ".mp4", ".md"})

    def ingest(self, batch: Any, ctx: IngestContext) -> None:
        super().ingest(batch, ctx)
        for resource in batch:
            if resource.relpath.endswith("/info.md"):
                folder = resource.relpath.rpartition("/")[0]
                studio = ctx.find(Studio, title=folder.partition(" (")[0])[0]
                other = dict(read_front_matter(resource.path)["other"])
                ctx.update(studio, country=other.pop("country", None))
                ctx.extra(studio, other)


class AliasEnv(Env):
    def scan(
        self, ignored: frozenset[str] | None = None, aliases: dict[str, str] | None = None
    ) -> ScanReport:
        self.clock += 1
        when = datetime.datetime(2026, 10, 26, tzinfo=datetime.UTC) + datetime.timedelta(
            minutes=self.clock
        )
        root = RootConfig("r", "Studios", str(self.files))
        return scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=InfoStudios,
            schema=self.schema, note_ignore_keys=ignored, note_key_aliases=aliases or {},
        )  # fmt: skip

    def aurora(self) -> dict[str, Any]:
        return self.item("Aurora (2001)")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[AliasEnv]:
    files = tmp_path / "files"
    (files / "Aurora (2001)").mkdir(parents=True)
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "S", ThemeRef(InfoStudios.id, 1)))
    schema = open_theme(engine, keep, InfoStudios).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        environment = AliasEnv(writer, reader, schema, files)
        environment.scan()
        yield environment
    reader.dispose()
    engine.dispose()


def test_a_files_aliased_key_fills_its_field_from_the_next_scan(env: AliasEnv) -> None:
    env.note(INFO, "---\nnation: Iceland\nmood: calm\n---\n")
    env.scan()
    assert env.aurora()["extra"] == {"nation": "Iceland", "mood": "calm"}

    env.scan(aliases={"nation": "Country"})  # the file is unchanged: read again all the same
    aurora = env.aurora()
    assert aurora["country"] == "Iceland"
    assert aurora["sources"]["country"] == FieldSource.EXTRACTED
    assert aurora["extra"] == {"mood": "calm"}

    env.scan(aliases={})  # no longer aliased: an Extra field again
    aurora = env.aurora()
    assert aurora["country"] is None
    assert aurora["extra"] == {"nation": "Iceland", "mood": "calm"}


def test_the_files_own_key_wins_over_its_alias(env: AliasEnv) -> None:
    env.note(INFO, "---\nnation: Iceland\ncountry: Norway\n---\n")
    env.scan(aliases={"nation": "country"})
    aurora = env.aurora()
    assert aurora["country"] == "Norway"
    assert aurora["extra"] == {"nation": "Iceland"}


def test_a_note_fills_the_field_at_the_next_scan(env: AliasEnv) -> None:
    env.note(NOTE, "---\nnation: Iceland\n---\nAbout Aurora.\n")
    env.scan()
    assert env.aurora()["extra"] == {"nation": "Iceland"}
    env.scan(aliases={"nation": "country"})
    aurora = env.aurora()
    assert aurora["country"] == "Iceland"
    assert aurora["sources"]["country"] == FieldSource.NOTE
    assert aurora["extra"] == {}


def test_the_aliases_read_with_are_remembered(env: AliasEnv) -> None:
    def applied() -> dict[str, str]:
        with env.reader.connect() as conn:
            return applied_aliases(conn)

    env.scan(aliases={"nation": "country"})
    assert applied() == {"nation": "country"}
    assert not env.writer.run(lambda conn: sync_aliases(conn, {"nation": "country"}))
    assert env.writer.run(lambda conn: sync_aliases(conn, {}))
    assert applied() == {}


def test_write_back_keeps_the_aliased_key() -> None:
    """An edited field the file holds under an alias is written there, not under its name;
    with the field's own key in the file too, that one is."""
    edited = frozenset({"country"})
    aliases = {"nation": "country"}
    wanted = {"country": "Norway"}
    assert aliased_keys(Studio, wanted, edited, {"nation": "Iceland"}, aliases) == {
        "nation": "Norway"
    }
    both = {"nation": "Iceland", "country": "Iceland"}
    assert aliased_keys(Studio, wanted, edited, both, aliases) == wanted
    assert aliased_keys(Studio, wanted, frozenset(), {"nation": "Iceland"}, aliases) == wanted
