"""scripts/profile_scan.py (#131): it runs end to end, and phases add up by name."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "profile_scan.py"


@pytest.fixture
def profile_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("profile_scan", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "SCRATCH", tmp_path / "profile")
    return module


def test_phases_of_one_kind_add_up(profile_scan: ModuleType) -> None:
    phases = [("Walking", 1.0), ("Ingesting 1 of 2", 2.0), ("Ingesting 2 of 2", 3.0), ("x", 0)]
    assert profile_scan.phase_totals(phases) == [("Walking", 1.0), ("Ingesting", 5.0)]


def test_profiles_a_folder(
    profile_scan: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = tmp_path / "files" / "Artist"
    files.mkdir(parents=True)
    for n, color in enumerate(("red", "blue", "green")):
        Image.new("RGB", (32, 24), color).save(files / f"p{n}.png")
    folder = str(tmp_path / "files")
    assert (
        profile_scan.main([folder, "--thumbnails", "5", "--threads", "1", "2", "--cprofile"]) == 0
    )
    out = capsys.readouterr().out
    assert "3 files; theme assets2d" in out
    assert "First scan:" in out
    assert "Rescan (nothing changed):" in out
    assert "Where the time went" in out
    assert " 1 thread :" in out
    assert " 2 threads:" in out
    assert (tmp_path / "profile" / "assets2d.keep").is_dir()
