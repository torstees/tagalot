"""Shared pytest configuration."""

import os
import urllib.request
from pathlib import Path

import pytest

from tagalot.core import backup_schedule, color_schemes, settings
from tagalot.themes import loader

# GUI tests must run headless. This runs before pytest-qt creates the QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def private_user_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the per-user settings file and themes folder into ``tmp_path``, so no test (or
    code a test runs, such as ``tagalot.ui.app.run``) reads or writes the real ones;
    the user's color schemes too (#572)."""
    user = tmp_path / "user-folders"
    monkeypatch.setattr(settings, "default_settings_path", lambda: user / "settings.toml")
    monkeypatch.setattr(loader, "default_user_themes_dir", lambda: user / "themes")
    monkeypatch.setattr(color_schemes, "default_schemes_dir", lambda: user / "color_schemes")
    return user


@pytest.fixture(autouse=True)
def no_system_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheduled backups (#611) never reach the real Task Scheduler, launchd, systemd, or
    crontab in tests: a test gives a scheduler its own fake runner."""

    def refuse(args: object, stdin: object = None) -> tuple[int, str]:
        raise AssertionError(f"a test tried to run {args!r}")

    monkeypatch.setattr(backup_schedule, "run_command", refuse)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Online lookups (#339) never go online in tests: they answer from a table."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a test tried to go online")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)


@pytest.fixture(autouse=True)
def no_user_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see the user's own ``TAGALOT_*`` variables (a TMDb token, #544): with
    one set, a keep would have something to look up online, and ask."""
    for name in list(os.environ):
        if name.upper().startswith("TAGALOT_"):
            monkeypatch.delenv(name)
