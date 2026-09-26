"""Smoke tests for the package skeleton."""

import importlib

import pytest

import tagalot
from tagalot.__main__ import main


def test_version() -> None:
    assert tagalot.__version__ == "0.1.0"


def test_main_returns_success() -> None:
    assert main() == 0


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
