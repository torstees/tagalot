"""``tagalot --export-tags`` (#609): the window's tag export, from the command line."""

import importlib.util
import io
import sys
from pathlib import Path

import pytest
from PIL import Image

from tagalot.__main__ import main
from tagalot.core.db import open_keep_database
from tagalot.core.exports import ExportError, export_tag_file, read_keep
from tagalot.core.keep import KEEP_FORMAT_VERSION, KEEP_TOML, open_keep
from tagalot.core.settings import Settings
from tagalot.core.tag_pictures import prepare_picture, set_tag_picture
from tagalot.core.tag_yaml import ImportPlan, plan_import
from tagalot.core.tags import TagTree

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def demo(tmp_path: Path) -> Path:
    spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "make_demo_keep.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keep: Path = module.make_demo(tmp_path / "scratch")
    return keep


def with_a_picture(keep_dir: Path) -> str:
    """Give the first top-level tag a picture; returns the tag's name."""
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 40, 40)).save(buffer, "PNG")
    _, engine = open_keep_database(open_keep(keep_dir))
    try:
        with engine.begin() as conn:
            tree = TagTree.load(conn)
            first = tree.children(None)[0]
            set_tag_picture(conn, first, prepare_picture(buffer.getvalue()))
            return tree.node(first).name
    finally:
        engine.dispose()


def plan(keep_dir: Path, file: Path) -> ImportPlan:
    opened = read_keep(keep_dir, Settings())
    try:
        with opened.reader.connect() as conn:
            return plan_import(
                conn, file.read_text(encoding="utf-8"), opened.schema, base=file.parent
            )
    finally:
        opened.reader.dispose()


def test_the_file_imports_back_unchanged(demo: Path, tmp_path: Path) -> None:
    name = with_a_picture(demo)
    target = tmp_path / "out" / "Demo tags.yaml"
    done = export_tag_file(demo, target, Settings())
    assert done.path == target
    assert done.items is None
    assert done.pictures == 1
    assert done.watched is None
    assert (tmp_path / "out" / "Demo tags pictures" / f"{name}.jpg").is_file()
    text = target.read_text(encoding="utf-8")
    assert f"picture: Demo tags pictures/{name}.jpg" in text
    assert "items:" not in text
    found = plan(demo, target)
    assert not found.changes
    assert not found.problems
    again = export_tag_file(demo, target, Settings())  # outside watched folders: replaced
    assert again.tags == done.tags


def test_with_items(demo: Path, tmp_path: Path) -> None:
    target = tmp_path / "Demo tags and items.yaml"
    done = export_tag_file(demo, target, Settings(), items=True)
    assert done.items
    found = plan(demo, target)
    assert found.items == done.items
    assert not found.not_found
    assert not found.changes


def test_watched_folders(demo: Path) -> None:
    files = Path(open_keep(demo).config.roots[0].path)
    target = files / "tags.yaml"
    with pytest.raises(ExportError, match="--allow-watched"):
        export_tag_file(demo, target, Settings())
    assert not target.exists()
    done = export_tag_file(demo, target, Settings(), allow_watched=True)
    assert done.watched
    before = target.read_bytes()
    with pytest.raises(ExportError, match="never replaces"):
        export_tag_file(demo, target, Settings(), allow_watched=True)
    assert target.read_bytes() == before


def test_a_keep_needing_an_upgrade_is_left_alone(demo: Path, tmp_path: Path) -> None:
    toml = demo / KEEP_TOML
    old = toml.read_text(encoding="utf-8").replace(
        f"format_version = {KEEP_FORMAT_VERSION}", f"format_version = {KEEP_FORMAT_VERSION - 1}"
    )
    toml.write_text(old, encoding="utf-8")
    db_before = (demo / "keep.db").stat().st_mtime_ns
    with pytest.raises(ExportError, match="open it in Tagalot once"):
        export_tag_file(demo, tmp_path / "tags.yaml", Settings())
    assert toml.read_text(encoding="utf-8") == old
    assert (demo / "keep.db").stat().st_mtime_ns == db_before
    assert not (tmp_path / "tags.yaml").exists()


def test_what_isnt_exported(demo: Path, tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="is not a keep"):
        export_tag_file(tmp_path, tmp_path / "tags.yaml", Settings())
    with pytest.raises(ExportError, match="is a folder"):
        export_tag_file(demo, tmp_path, Settings())


def test_the_command_line(
    demo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("tagalot.backup_tool.load_settings", Settings)  # not the real one
    target = tmp_path / "tags.yaml"
    monkeypatch.setattr(
        sys, "argv", ["tagalot", "--export-tags", str(demo), "--to", str(target), "--items"]
    )
    assert main() == 0
    out = capsys.readouterr().out
    assert out.startswith("ok  exported ")
    assert " items to " in out
    assert target.is_file()
    monkeypatch.setattr(sys, "argv", ["tagalot", "--export-tags", str(demo)])
    assert main() == 2
    assert capsys.readouterr().out.startswith("error: --to FILE is needed")
    watched = Path(open_keep(demo).config.roots[0].path) / "tags.yaml"
    monkeypatch.setattr(sys, "argv", ["tagalot", "--export-tags", str(demo), "--to", str(watched)])
    assert main() == 2
    assert "--allow-watched" in capsys.readouterr().out
    assert not watched.exists()
