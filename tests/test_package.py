"""Smoke tests for the package skeleton."""

import importlib
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

import tagalot
from tagalot.__main__ import main
from tagalot.ui import app


def test_version() -> None:
    """The code and pyproject.toml agree (packaged builds and release tags use it)."""
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text("utf-8"))
    assert tagalot.__version__ == pyproject["project"]["version"]


def test_main_runs_the_ui_with_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Sequence[str]] = []

    def fake_run(argv: Sequence[str]) -> int:
        calls.append(argv)
        return 3

    monkeypatch.setattr(app, "run", fake_run)
    assert main() == 3
    assert calls == [sys.argv]


@pytest.mark.parametrize(
    "module",
    [
        "PySide6.QtWidgets",
        "sqlalchemy",
        "PIL",
        "mutagen",
        "py7zr",
        "rarfile",
        "platformdirs",
        "tomli_w",
    ],
)
def test_runtime_dependencies_import(module: str) -> None:
    importlib.import_module(module)
