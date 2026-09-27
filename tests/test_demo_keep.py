"""The demo keep script used by manual-testing checklists must keep working."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import select

from tagalot.core.models import Entity
from tagalot.core.search import count_by_type, run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import TagTree
from tagalot.themes.loader import load_themes

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make_demo_keep.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_creates_a_scanned_and_tagged_keep(tmp_path: Path) -> None:
    keep_dir = _script().make_demo(tmp_path)
    assert keep_dir == tmp_path / "Demo.keep"
    with KeepSession.open(keep_dir, Settings()) as session:
        tree = session.tag_cache.get()
        with session.reader.connect() as conn:
            titles = set(conn.scalars(select(Entity.title)))
            places = run_search(conn, SearchSpec(include=_ids(tree, "Places")), tree)
            money = run_search(conn, SearchSpec(include=_ids(tree, "Money")), tree)
        [report] = session.scan_all()
    # The .DS_Store, ._ and Thumbs.db files are on disk but skipped.
    assert len(titles) == 9
    assert "._sunset.jpg" not in titles
    assert (tmp_path / "demo-files" / "Photos" / "Beach" / "._sunset.jpg").exists()
    assert len(places) == 5  # Iceland's three and Beach's two, through the hierarchy
    assert {h.title for h in money} == {"tax 2025.pdf", "März.txt"}
    assert [s.alias for s in tree.suggest("finance")] == ["finance"]
    assert report.online
    assert report.new == report.ingested == 0  # already scanned
    assert report.ingest_errors == []


def _ids(tree: TagTree, name: str) -> tuple[int, ...]:
    [suggestion] = [s for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
    return (suggestion.tag_id,)


def test_refuses_to_overwrite_without_reset(tmp_path: Path) -> None:
    script = _script()
    script.make_demo(tmp_path)
    with pytest.raises(FileExistsError, match="--reset"):
        script.make_demo(tmp_path)
    (tmp_path / "demo-files" / "extra.txt").write_text("x", encoding="utf-8")
    script.make_demo(tmp_path, reset=True)
    assert not (tmp_path / "demo-files" / "extra.txt").exists()  # reset starts clean


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows locks open files")
def test_reset_refuses_while_the_keep_is_open(tmp_path: Path) -> None:
    script = _script()
    keep_dir = script.make_demo(tmp_path)
    with KeepSession.open(keep_dir, Settings()) as session:
        session.scan_all()
        with pytest.raises(script.DemoInUseError, match="Nothing was deleted"):
            script.make_demo(tmp_path, reset=True)
        assert (keep_dir / "keep.toml").exists()  # untouched, not half-deleted
    assert script.make_demo(tmp_path, reset=True) == keep_dir  # fine once it's closed


def test_a_failed_reset_puts_everything_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script()
    script.make_demo(tmp_path)
    rename = Path.rename

    def busy_files(self: Path, target: Path) -> Path:
        if self.name == "demo-files":
            raise PermissionError(13, "The process cannot access the file")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", busy_files)
    with pytest.raises(script.DemoInUseError, match="demo-files is in use"):
        script.make_demo(tmp_path, reset=True)
    assert (tmp_path / "Demo.keep" / "keep.toml").exists()  # renamed aside, then back
    assert not (tmp_path / "Demo.keep.deleting").exists()
    assert (tmp_path / "demo-files" / "readme.md").exists()


def test_creates_a_media_keep_with_several_types(tmp_path: Path) -> None:
    script = _script()
    themes = tmp_path / "themes"
    keep_dir = script.make_media_demo(tmp_path, themes_dir=themes)
    assert (themes / script.MEDIA_THEME_FILE).exists()
    catalog = load_themes(user_dir=themes)
    with KeepSession.open(keep_dir, Settings(), catalog=catalog) as session:
        tree = session.tag_cache.get()
        with session.reader.connect() as conn:
            everything = count_by_type(conn, SearchSpec(), tree)
            love = count_by_type(conn, SearchSpec(text="love"), tree)
            rock = count_by_type(conn, SearchSpec(include=_ids(tree, "Rock")), tree)
    assert everything == {"demo_media.artist": 5, "demo_media.album": 11, "demo_media.song": 6}
    assert love == {"demo_media.artist": 2, "demo_media.album": 6, "demo_media.song": 2}
    assert rock == {"demo_media.artist": 2, "demo_media.album": 2, "demo_media.song": 2}
    with pytest.raises(FileExistsError, match="--reset"):
        script.make_media_demo(tmp_path, themes_dir=themes)
    assert script.make_media_demo(tmp_path, reset=True, themes_dir=themes) == keep_dir
