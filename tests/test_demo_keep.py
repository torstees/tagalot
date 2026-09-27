"""The demo keep script used by manual-testing checklists must keep working."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make_demo_keep.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_creates_a_keep_that_scans_cleanly(tmp_path: Path) -> None:
    keep_dir = _script().make_demo(tmp_path)
    assert keep_dir == tmp_path / "Demo.keep"
    with KeepSession.open(keep_dir, Settings()) as session:
        [report] = session.scan_all()
    assert report.online
    assert report.new == report.ingested == 9  # the .DS_Store, ._ and Thumbs.db files are skipped
    assert (tmp_path / "demo-files" / "Photos" / "Beach" / "._sunset.jpg").exists()
    assert report.ingest_errors == []


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
