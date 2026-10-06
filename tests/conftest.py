"""Shared pytest configuration."""

import os
import urllib.request
from pathlib import Path

import pytest

from tagalot.core import settings
from tagalot.themes import loader

# GUI tests must run headless. This runs before pytest-qt creates the QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def private_user_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the per-user settings file and themes folder into ``tmp_path``, so no test (or
    code a test runs, such as ``tagalot.ui.app.run``) reads or writes the real ones."""
    user = tmp_path / "user-folders"
    monkeypatch.setattr(settings, "default_settings_path", lambda: user / "settings.toml")
    monkeypatch.setattr(loader, "default_user_themes_dir", lambda: user / "themes")
    return user


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Online lookups (#339) never go online in tests: they answer from a table."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a test tried to go online")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
