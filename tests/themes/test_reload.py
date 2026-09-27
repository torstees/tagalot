"""Tests for resolving a keep's theme and reloading it without restarting (issue #47), and
for a broken theme not affecting keeps that use other themes (issue #50)."""

import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Connection, Engine, insert, inspect, select

from tagalot.builtin_themes.generic import GenericTheme
from tagalot.core.db import KeepNeedsMigration, open_keep_database
from tagalot.core.keep import Keep, ThemeRef, create_keep
from tagalot.core.models import Entity
from tagalot.core.theme_db import KeepThemeError, open_theme, reload_theme, resolve_theme
from tagalot.themes.api import IngestContext
from tagalot.themes.loader import load_themes

V1 = """
from tagalot.themes.api import Entity, Theme, field

class Clip(Entity):
    length: int | None = field("Length", search="range")

class Clips(Theme):
    id, name, version = "clips", "Clips", 1
    entities = [Clip]
"""

V1_WITH_FIELD = V1.replace(
    '    length: int | None = field("Length", search="range")',
    '    length: int | None = field("Length", search="range")\n'
    '    camera: str | None = field("Camera", search="choice")',
)

V2 = (
    V1_WITH_FIELD.replace('"Clips", 1', '"Clips", 2')
    + """
    def migrate(self, from_version, ctx):
        MIGRATED.append(from_version)

MIGRATED = []
"""
)


def _write(folder: Path, source: str) -> Path:
    path = folder / "clips.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


@pytest.fixture
def themes(tmp_path: Path) -> Path:
    folder = tmp_path / "themes"
    folder.mkdir()
    _write(folder, V1)
    return folder


@pytest.fixture
def keep(tmp_path: Path, themes: Path) -> Iterator[tuple[Keep, Engine]]:
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("clips", 1)))
    catalog = load_themes(user_dir=themes)
    keep = open_theme(engine, keep, resolve_theme(catalog, keep)).keep
    yield keep, engine
    engine.dispose()


def _columns(engine: Engine) -> list[str]:
    return [c["name"] for c in inspect(engine).get_columns("clips_clip")]


def test_resolve_a_builtin_theme(tmp_path: Path) -> None:
    keep = create_keep(tmp_path / "g", "G", ThemeRef("generic", 1))
    assert resolve_theme(load_themes(user_dir=tmp_path / "none"), keep) is GenericTheme


def test_resolve_a_missing_theme(tmp_path: Path) -> None:
    keep = create_keep(tmp_path / "m", "M", ThemeRef("movies", 1))
    with pytest.raises(KeepThemeError, match="uses the 'movies' theme, which isn't available"):
        resolve_theme(load_themes(user_dir=tmp_path / "none"), keep)


def test_reload_picks_up_an_added_field(keep: tuple[Keep, Engine], themes: Path) -> None:
    k, engine = keep
    with engine.begin() as conn:
        conn.execute(insert(Entity).values(id=1, type="clips.clip", title="Beach"))
    assert _columns(engine) == ["id", "length"]

    _write(themes, V1_WITH_FIELD)
    opened, _ = reload_theme(engine, k, user_dir=themes)
    assert _columns(engine) == ["id", "length", "camera"]
    assert opened.changes.columns == ["clips_clip.camera"]
    assert [f.name for f in next(iter(opened.schema.entities.values())).fields] == [
        "length",
        "camera",
    ]
    with engine.connect() as conn:
        assert conn.scalar(select(Entity.title)) == "Beach"  # data kept


def test_a_broken_edit_changes_nothing(keep: tuple[Keep, Engine], themes: Path) -> None:
    k, engine = keep
    path = _write(themes, V1_WITH_FIELD.replace("class Clips(Theme):", "class Clips(Theme:"))
    with pytest.raises(KeepThemeError) as info:
        reload_theme(engine, k, user_dir=themes)
    assert str(path) in str(info.value)
    assert "SyntaxError" in str(info.value)
    assert _columns(engine) == ["id", "length"]  # the keep still works with the old schema


def test_a_version_bump_asks_before_migrating(keep: tuple[Keep, Engine], themes: Path) -> None:
    k, engine = keep
    _write(themes, V2)
    with pytest.raises(KeepNeedsMigration) as info:
        reload_theme(engine, k, user_dir=themes)
    assert (info.value.component, info.value.stored, info.value.current) == ("clips", 1, 2)

    def context(conn: Connection) -> IngestContext:
        return cast(IngestContext, object())

    opened, catalog = reload_theme(
        engine, k, user_dir=themes, allow_migration=True, make_context=context
    )
    assert opened.migrated_from == 1
    loaded = catalog.get("clips")
    assert loaded is not None
    module = __import__(loaded.theme.__module__, fromlist=["MIGRATED"])
    assert module.MIGRATED == [1]
    assert opened.keep.config.theme.version == 2


def test_a_removed_theme_file(keep: tuple[Keep, Engine], themes: Path) -> None:
    k, engine = keep
    (themes / "clips.py").unlink()
    with pytest.raises(KeepThemeError, match="isn't available"):
        reload_theme(engine, k, user_dir=themes)


def test_a_broken_theme_does_not_affect_keeps_using_other_themes(tmp_path: Path) -> None:
    folder = tmp_path / "themes"
    folder.mkdir()
    (folder / "broken.py").write_text(
        "raise RuntimeError('this theme is broken')\n", encoding="utf-8"
    )
    _write(folder, V1.replace("class Clip(Entity):", "class Clip(Entity):\n    when: 'date' = 1"))
    catalog = load_themes(user_dir=folder)
    assert len(catalog.problems) == 2  # both user themes are broken...

    keep, engine = open_keep_database(create_keep(tmp_path / "g", "G", ThemeRef("generic", 1)))
    try:
        opened = open_theme(engine, keep, resolve_theme(catalog, keep))  # ...generic still opens
        assert "generic_file" in inspect(engine).get_table_names()
        assert opened.schema.theme is GenericTheme
    finally:
        engine.dispose()
