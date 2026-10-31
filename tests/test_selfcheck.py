"""``tagalot --check`` and ``--version`` (#129): what packaged builds run in CI."""

import subprocess
import sys
from pathlib import Path

import pytest

import tagalot
from tagalot import selfcheck
from tagalot.__main__ import main


def test_everything_is_there_from_source() -> None:
    lines: list[str] = []
    assert selfcheck.run_check(lines.append) == 0
    assert lines[0] == f"Tagalot {tagalot.__version__}"
    assert lines[-1] == "All good."
    themes = next(line for line in lines if line.startswith("Themes"))
    for theme in selfcheck.BUILTIN:
        assert theme in themes


def test_a_missing_reader_fails_unless_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> str:
        raise ImportError("no module named pymediainfo")

    readers = [
        ("MediaInfo", broken, True),
        ("RAR", broken, False),
    ]
    monkeypatch.setattr(selfcheck, "READERS", readers)
    lines: list[str] = []
    assert selfcheck.run_check(lines.append) == 1
    assert any(line.startswith("MediaInfo") and "MISSING" in line for line in lines)
    assert any(line.startswith("RAR") and "unavailable" in line for line in lines)
    assert lines[-1] == "Missing: MediaInfo"  # an optional one doesn't fail the check


def test_the_command_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["tagalot", "--version"])
    assert main() == 0
    assert capsys.readouterr().out == f"Tagalot {tagalot.__version__}\n"
    monkeypatch.setattr(sys, "argv", ["tagalot", "--check"])
    assert main() == 0
    assert capsys.readouterr().out.endswith("All good.\n")


def test_it_runs_without_a_display() -> None:
    """As CI runs it on a packaged build: a process of its own, no window."""
    done = subprocess.run(
        [sys.executable, "-m", "tagalot", "--check"], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "All good." in done.stdout


def test_the_console_twin_only_runs_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "tagalot-cli.exe"))
    monkeypatch.setattr(sys, "argv", ["tagalot-cli"])
    assert main() == 2  # no window from the console executable (#610)
    assert capsys.readouterr().out.startswith("usage: tagalot-cli --backup KEEP")
    monkeypatch.setattr(sys, "argv", ["tagalot-cli", "--version"])
    assert main() == 0
    assert capsys.readouterr().out == f"Tagalot {tagalot.__version__}\n"


def test_a_windows_build_reports_its_console_twin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "tagalot.exe"))
    monkeypatch.setattr(selfcheck, "READERS", [])
    lines: list[str] = []
    assert selfcheck.run_check(lines.append) == 1
    assert "Console        MISSING      tagalot-cli.exe" in lines
    (tmp_path / "tagalot-cli.exe").write_bytes(b"")
    lines.clear()
    assert selfcheck.run_check(lines.append) == 0
    assert "Console        ok           tagalot-cli.exe" in lines
