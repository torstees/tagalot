"""Theme options: declaring, keep.toml, the values per root, and ingest's new context
methods (#82)."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, select

from tagalot.core.db import open_keep_database
from tagalot.core.ingest import IngestError, IngestSession
from tagalot.core.keep import (
    KeepConfig,
    KeepConfigError,
    RootConfig,
    ThemeRef,
    create_keep,
    dump_keep_config,
    load_keep_config,
)
from tagalot.core.models import Entity, EntityContains
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_options import effective_options
from tagalot.core.writer import DbWriter
from tagalot.themes.api import Entity as ThemeEntity
from tagalot.themes.api import Theme, ThemeDeclarationError, contains, option
from tagalot.themes.loader import validate_theme


class Box(ThemeEntity):
    pass


class Thing(ThemeEntity):
    pass


class OptionsTheme(Theme):
    id, name = "opts", "Options"
    entities = [Box, Thing]
    containment = [contains(Box, Thing)]
    options = [
        option("level", 1, label="Level"),
        option("scale", 1.5, label="Scale"),
        option("strict", False, label="Strict"),
        option("prefix", "x", label="Prefix"),
    ]


# --- declaring ---


def test_option_types_come_from_the_default() -> None:
    assert [(o.name, o.type) for o in OptionsTheme.options] == [
        ("level", int),
        ("scale", float),
        ("strict", bool),
        ("prefix", str),
    ]
    with pytest.raises(ThemeDeclarationError, match="bool, int, float, or str"):
        option("bad", [1], label="Bad")
    with pytest.raises(ThemeDeclarationError, match="identifier"):
        option("not ok", 1, label="Bad")


def test_the_loader_checks_options() -> None:
    assert validate_theme(OptionsTheme) == []

    class Twice(OptionsTheme):
        options = [option("a", 1, label="A"), option("a", 2, label="A")]

    class Loose(OptionsTheme):
        options = [{"name": "a"}]  # type: ignore[list-item]

    assert "an option is declared twice" in validate_theme(Twice)
    assert "options must be declared with option()" in validate_theme(Loose)


# --- values per root ---


@pytest.mark.parametrize(
    ("keep", "root", "values", "problems"),
    [
        ({}, {}, {"level": 1, "scale": 1.5, "strict": False, "prefix": "x"}, []),
        ({"level": 2}, {}, {"level": 2}, []),
        ({"level": 2}, {"level": 3}, {"level": 3}, []),  # the root wins
        ({"scale": 2}, {}, {"scale": 2.0}, []),  # a whole number is fine for a float
        ({"level": "2"}, {}, {"level": 1}, ["level must be a whole number, not '2'"]),
        ({"level": True}, {}, {"level": 1}, ["level must be a whole number, not True"]),
        ({}, {"strict": 1}, {"strict": False}, ["strict must be true or false"]),
        ({"colour": "red"}, {}, {}, ["the Options theme has no option 'colour'"]),
    ],
)
def test_effective_options(
    keep: dict[str, Any], root: dict[str, Any], values: dict[str, Any], problems: list[str]
) -> None:
    found = effective_options(OptionsTheme, keep, root)
    assert {k: found.values[k] for k in values} == values
    assert len(found.problems) == len(problems)
    for problem, expected in zip(found.problems, problems, strict=True):
        assert expected in problem


def test_the_fingerprint_changes_with_the_values() -> None:
    a = effective_options(OptionsTheme, {}, {}).fingerprint()
    assert effective_options(OptionsTheme, {}, {}).fingerprint() == a
    assert effective_options(OptionsTheme, {"level": 2}, {}).fingerprint() != a


# --- keep.toml ---


def _config() -> KeepConfig:
    return KeepConfig(
        id=uuid.UUID(int=1),
        name="K",
        theme=ThemeRef("opts", 1),
        roots=[RootConfig("r", "R", "/r", options={"level": 2, "prefix": "it's"})],
        theme_options={"level": 1, "strict": True, "scale": 0.5},
    )


def test_options_round_trip(tmp_path: Path) -> None:
    text = dump_keep_config(_config())
    assert "[theme.options]\nlevel = 1\nstrict = true\nscale = 0.5\n" in text
    assert 'options = { level = 2, prefix = "it\'s" }' in text
    path = tmp_path / "keep.toml"
    path.write_text(text, encoding="utf-8")
    assert load_keep_config(path) == _config()


def test_no_options_writes_nothing() -> None:
    config = _config()
    config.theme_options, config.roots[0].options = {}, {}
    text = dump_keep_config(config)
    assert "options" not in text


@pytest.mark.parametrize(
    "bad",
    ["[theme.options]\nlevel = [1]\n", "[theme.options]\nlevel = { deep = 1 }\n"],
)
def test_option_values_must_be_scalars(tmp_path: Path, bad: str) -> None:
    config = _config()
    config.theme_options = {}
    text = dump_keep_config(config).replace("[[roots]]", bad + "\n[[roots]]")
    path = tmp_path / "keep.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(KeepConfigError, match=r"\[theme.options\] must be a table"):
        load_keep_config(path)


# --- ingest's context ---


@pytest.fixture
def writer(tmp_path: Path) -> Any:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("opts", 1)))
    schema = open_theme(engine, keep, OptionsTheme).schema
    with DbWriter(engine) as writer:
        writer.schema = schema  # type: ignore[attr-defined]
        yield writer


def test_option_reads_the_values_given_or_the_defaults(writer: Any) -> None:
    def work(conn: Connection) -> tuple[Any, Any]:
        given = IngestSession(conn, writer.schema, options={"level": 4})
        plain = IngestSession(conn, writer.schema)
        with pytest.raises(IngestError, match="no option 'nope'"):
            plain.option("nope")
        return given.option("level"), plain.option("prefix")

    assert writer.run(work) == (4, "x")


def test_contents_linked_and_delete(writer: Any) -> None:
    def work(conn: Connection) -> dict[str, Any]:
        ctx = IngestSession(conn, writer.schema)
        box = ctx.upsert(Box, "box", title="Box")
        a, b = ctx.upsert(Thing, "a", title="A"), ctx.upsert(Thing, "b", title="B")
        ctx.contain(box, a)
        ctx.flush()
        ctx.contain(box, b)  # pending until flush: contents counts it
        ctx.uncontain(box, a)
        pending = [e.id for e in ctx.contents(box)]
        ctx.flush()
        flushed = [e.id for e in ctx.contents(box)]
        ctx.uncontain(box, b)
        empty = ctx.contents(box)
        ctx.delete(box)
        ctx.flush()
        return {
            "pending": pending == [b.id],
            "flushed": flushed == [b.id],
            "empty": empty,
            "linked": ctx.linked(a),
            "left": sorted(conn.scalars(select(Entity.title))),
            "edges": conn.scalars(select(EntityContains.parent_id)).all(),
        }

    found = writer.run(work)
    assert found == {
        "pending": True,
        "flushed": True,
        "empty": [],
        "linked": [],
        "left": ["A", "B"],
        "edges": [],
    }
