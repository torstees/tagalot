"""Enforce the import rules from AGENTS.md by parsing the source tree."""

import ast
import importlib
import importlib.util
import pkgutil
from collections.abc import Iterator
from pathlib import Path

import pytest

import tagalot

PACKAGE_DIR = Path(tagalot.__file__).parent
QT_PACKAGES = ("PySide6", "shiboken6", "PyQt5", "PyQt6")


def _module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE_DIR.parent).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path) -> Iterator[str]:
    """Yield the absolute module names imported by a source file."""
    package = _module_name(path)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    yield from _imports_in(path.read_text(encoding="utf-8"), package)


def _imports_in(source: str, package: str) -> Iterator[str]:
    """Yield absolute module names imported by `source`, which lives in `package`."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            else:
                base = node.module or ""
            if node.module:
                yield base
            else:  # `from . import x` imports submodules
                yield from (f"{base}.{alias.name}" for alias in node.names)


def _sources(subpackage: str) -> list[Path]:
    return sorted((PACKAGE_DIR / subpackage).rglob("*.py"))


def _is_within(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def test_every_module_imports() -> None:
    names = [m.name for m in pkgutil.walk_packages(tagalot.__path__, "tagalot.")]
    assert "tagalot.core.models" in names
    for name in names:
        importlib.import_module(name)


@pytest.mark.parametrize("path", _sources("core"), ids=lambda p: p.name)
def test_core_never_imports_qt_or_ui(path: Path) -> None:
    bad = [
        m
        for m in _imports(path)
        if _is_within(m, "tagalot.ui") or any(_is_within(m, qt) for qt in QT_PACKAGES)
    ]
    assert not bad, f"{path} imports {bad}"


@pytest.mark.parametrize("path", _sources("builtin_themes"), ids=lambda p: p.name)
def test_builtin_themes_import_only_theme_api(path: Path) -> None:
    bad = [
        m
        for m in _imports(path)
        if _is_within(m, "tagalot") and not _is_within(m, "tagalot.themes.api")
    ]
    assert not bad, f"{path} imports {bad}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import PySide6.QtCore", ["PySide6.QtCore"]),
        ("from tagalot.ui import app", ["tagalot.ui"]),
        ("from ..ui import main_window", ["tagalot.ui"]),
        ("from . import tags", ["tagalot.core.tags"]),
        ("from .search import SearchSpec", ["tagalot.core.search"]),
    ],
)
def test_import_scanner_resolves_imports(source: str, expected: list[str]) -> None:
    # Guards the helper itself; a module inside tagalot.core is assumed.
    assert list(_imports_in(source, "tagalot.core")) == expected
