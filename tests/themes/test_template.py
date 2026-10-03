"""The theme template and the author tools (#130): the template is a working theme, and
``--new-theme`` / ``--check-theme`` help authors start and check one."""

import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import aliased

from tagalot.__main__ import main
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity, EntityContains
from tagalot.core.scanjob import scan_root
from tagalot.core.settings import Settings
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.theme_tools import (
    ThemeToolError,
    check_theme,
    check_theme_named,
    new_theme,
    template_source,
)
from tagalot.themes import template
from tagalot.themes.loader import validate_theme
from tagalot.themes.template import Group, Item, TemplateTheme

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def test_the_template_is_a_valid_theme() -> None:
    assert validate_theme(TemplateTheme) == []
    assert template_source().startswith('"""TODO: say what your theme organizes')
    assert "from tagalot." not in template_source().replace("from tagalot.themes.api", "")


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    files = tmp_path / "files"
    (files / "Recipes").mkdir(parents=True)
    (files / "Recipes" / "soup.txt").write_text("\n  Tomato soup  \nonions...", encoding="utf-8")
    (files / "Recipes" / "bread.md").write_text("", encoding="utf-8")
    (files / "Recipes" / "photo.jpg").write_bytes(b"not really a picture")
    (files / "loose.pdf").write_bytes(b"%PDF")
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "K", ThemeRef("template", 1)))
    schema = open_theme(engine, keep, TemplateTheme).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, files)
    reader.dispose()
    engine.dispose()


def test_the_template_turns_folders_and_files_into_groups_and_items(env: Env) -> None:
    root = RootConfig("r", "Files", str(env.files))
    report = scan_root(
        env.writer, env.reader, root, root.path, when=T0, theme=TemplateTheme, schema=env.schema
    )
    assert report.ingest_errors == []
    parent, child = aliased(Entity), aliased(Entity)
    table = env.schema.entities[Item].table
    with env.reader.connect() as conn:
        items: dict[str, str | None] = dict(
            conn.execute(
                select(Entity.title, table.c.headline).join(table, table.c.id == Entity.id)
            ).all()
        )
        groups = conn.scalars(
            select(Entity.title).where(Entity.type == TemplateTheme.type_id_of(Group))
        ).all()
        contained = sorted(
            conn.execute(
                select(parent.title, child.title)
                .join(EntityContains, EntityContains.parent_id == parent.id)
                .join(child, child.id == EntityContains.child_id)
            ).all()
        )
    assert items == {
        "soup.txt": "Tomato soup",  # its first non-empty line
        "bread.md": None,  # empty
        "photo.jpg": None,  # not text
        "loose.pdf": None,
    }
    assert groups == ["Recipes"]
    assert contained == [
        ("Recipes", "bread.md"),
        ("Recipes", "photo.jpg"),
        ("Recipes", "soup.txt"),
    ]  # loose.pdf is in no group: it's in the root


def test_group_folders_at_another_level() -> None:
    assert template.group_folder("Recipes/soup.txt", 1) == "Recipes"
    assert template.group_folder("Shelf/Recipes/soup.txt", 2) == "Shelf/Recipes"
    assert template.group_folder("soup.txt", 1) is None


def test_new_theme_fills_in_the_template(tmp_path: Path) -> None:
    path = new_theme("my_photos", "My photos", tmp_path)
    assert path == tmp_path / "my_photos.py"
    source = path.read_text(encoding="utf-8")
    assert "id = 'my_photos'" in source
    assert "name = 'My photos'" in source
    assert "class MyPhotosTheme(Theme):" in source
    lines: list[str] = []
    assert check_theme(path, lines.append) == 0
    assert lines[0].startswith("ok: 'my_photos' (My photos, version 1)")
    assert (
        new_theme("recipes", folder=tmp_path).read_text(encoding="utf-8").count("name = 'Recipes'")
        == 1
    )  # the name defaults from the id


@pytest.mark.parametrize(
    ("theme_id", "message"),
    [
        ("My Photos", "isn't a theme id"),
        ("2photos", "isn't a theme id"),
        ("class", "isn't a theme id"),
        ("music", "already a theme 'music'"),  # a built-in theme's id
    ],
)
def test_new_theme_refuses(tmp_path: Path, theme_id: str, message: str) -> None:
    with pytest.raises(ThemeToolError, match=message):
        new_theme(theme_id, folder=tmp_path)


def test_new_theme_wont_overwrite(tmp_path: Path) -> None:
    (tmp_path / "notes.py").write_text("# mine", encoding="utf-8")
    with pytest.raises(ThemeToolError, match="already"):
        new_theme("notes", folder=tmp_path)
    assert (tmp_path / "notes.py").read_text(encoding="utf-8") == "# mine"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("def broken(:\n", "SyntaxError"),
        ("x = 1\n", "defines no Theme subclass"),
        (
            "from tagalot.themes.api import Theme\n"
            "class T(Theme):\n    id, name = 'bad', 'Bad'\n    extensions = {'TXT'}\n",
            "extension 'TXT' must be lowercase",
        ),
    ],
)
def test_check_theme_reports_problems(tmp_path: Path, source: str, message: str) -> None:
    path = tmp_path / "bad.py"
    path.write_text(source, encoding="utf-8")
    lines: list[str] = []
    assert check_theme(path, lines.append) == 1
    assert any(message in line for line in lines), lines
    assert check_theme(tmp_path / "missing.txt", lines.append) == 1


def test_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("tagalot.theme_tools.default_user_themes_dir", lambda: tmp_path)
    monkeypatch.setattr("tagalot.theme_tools.load_settings", Settings)  # no extra folders
    monkeypatch.setattr(sys, "argv", ["tagalot", "--new-theme", "comics", "--name", "Comics"])
    assert main() == 0
    out = capsys.readouterr().out
    assert f"Wrote {tmp_path / 'comics.py'}" in out
    assert "tagalot --check-theme comics" in out  # the next step, by id
    for name in ("comics", str(tmp_path / "comics.py")):  # by id, or by path
        monkeypatch.setattr(sys, "argv", ["tagalot", "--check-theme", name])
        assert main() == 0
        assert capsys.readouterr().out.startswith("ok: 'comics' (Comics, version 1)")
    monkeypatch.setattr(sys, "argv", ["tagalot", "--new-theme"])
    assert main() == 2
    assert "Usage" in capsys.readouterr().out


def _check(name: str, folders: list[Path]) -> tuple[int, list[str]]:
    lines: list[str] = []
    return check_theme_named(name, folders, lines.append), lines


def test_check_a_theme_by_its_id(tmp_path: Path) -> None:
    extra = tmp_path / "extra"
    new_theme("recipes", folder=tmp_path)
    new_theme("comics", folder=extra)
    assert _check("recipes", [tmp_path, extra])[0] == 0
    code, lines = _check("comics", [tmp_path, extra])  # found in the second folder
    assert code == 0
    assert str(extra / "comics.py") in lines[0]


def test_by_the_id_a_file_declares(tmp_path: Path) -> None:
    """A file named otherwise is found by the id inside it."""
    source = template_source().replace('id = "template"', 'id = "odd"')
    (tmp_path / "my_file.py").write_text(source, encoding="utf-8")
    code, lines = _check("odd", [tmp_path])
    assert code == 0
    assert lines[0].startswith("ok: 'odd'")
    assert "my_file.py" in lines[0]


def test_a_built_in_theme_by_its_id(tmp_path: Path) -> None:
    code, lines = _check("music", [tmp_path])
    assert code == 0
    assert lines[0] == "ok: 'music' (Music, version 2), built in"


def test_an_id_no_theme_has(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    code, lines = _check("nope", [tmp_path])
    assert code == 1
    assert lines[0] == "problem: no theme 'nope': not a file, and no theme has that id"
    assert lines[1] == f"   looked in: {tmp_path}"
    assert any(line.strip() == str(tmp_path / "broken.py") for line in lines)  # maybe it
