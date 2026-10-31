"""Shared pytest configuration."""

import functools
import os
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pytestqt.qtbot import QtBot

from tagalot.core import backup_schedule, color_schemes, settings
from tagalot.themes import loader

# GUI tests must run headless. This runs before pytest-qt creates the QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

WAIT_SCALE = 3
"""Every pytest-qt wait gets this many times the timeout its test gives (#635): a test's
``timeout=5000`` waits up to 15 s. Slow runners (macOS CI) now and then took longer than 5 s
for nothing wrong; a test that is really stuck still fails, a little later (#374)."""


def _more_room(wait: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(wait)
    def waiting(self: QtBot, *args: Any, timeout: int | None = 5000, **kwargs: Any) -> Any:
        if timeout is not None and timeout > 0:  # none or 0: left as it is
            timeout *= WAIT_SCALE
        return wait(self, *args, timeout=timeout, **kwargs)

    return waiting


for _name in ("waitUntil", "waitSignal", "waitSignals", "waitCallback"):
    setattr(QtBot, _name, _more_room(getattr(QtBot, _name)))


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
