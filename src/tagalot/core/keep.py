"""Open and create keeps; ``keep.toml``."""

import logging
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tagalot.core.tomlio import toml_list, toml_str, write_atomic

logger = logging.getLogger(__name__)

KEEP_TOML = "keep.toml"
KEEP_FORMAT_VERSION = 1
"""Core format version written to new keeps (DESIGN.md §4)."""


class KeepConfigError(Exception):
    """A ``keep.toml`` file is missing, unreadable, or invalid."""

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


@dataclass
class ThemeRef:
    """The theme a keep uses and the theme schema version its database was built with."""

    id: str
    version: int


@dataclass
class RootConfig:
    """A watched directory. ``path`` is kept as written: it may be a UNC or drive path."""

    id: str
    name: str
    path: str
    exclude: list[str] = field(default_factory=list)


@dataclass
class KeepConfig:
    """The contents of ``keep.toml``: identity, theme, and roots."""

    id: uuid.UUID
    name: str
    theme: ThemeRef
    format_version: int = KEEP_FORMAT_VERSION
    roots: list[RootConfig] = field(default_factory=list)


def load_keep_config(path: Path) -> KeepConfig:
    """Read and validate a ``keep.toml`` file.

    Unknown keys are ignored with a warning so that older builds can open keeps written by
    newer ones. Raises :class:`KeepConfigError` for anything missing or malformed.
    """
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise KeepConfigError(path, f"cannot read file: {e.strerror or e}") from e
    except tomllib.TOMLDecodeError as e:
        raise KeepConfigError(path, f"invalid TOML: {e}") from e
    return _parse(path, data)


def save_keep_config(config: KeepConfig, path: Path) -> None:
    """Write ``config`` to ``path`` atomically (write a temp file, then replace)."""
    write_atomic(path, dump_keep_config(config))


def dump_keep_config(config: KeepConfig) -> str:
    """Serialize ``config`` as TOML, preferring literal strings so paths stay hand-editable."""
    lines = [
        "[keep]",
        f"id = {toml_str(str(config.id))}",
        f"name = {toml_str(config.name)}",
        f"format_version = {config.format_version}",
        "",
        "[theme]",
        f"id = {toml_str(config.theme.id)}",
        f"version = {config.theme.version}",
    ]
    for root in config.roots:
        lines += [
            "",
            "[[roots]]",
            f"id = {toml_str(root.id)}",
            f"name = {toml_str(root.name)}",
            f"path = {toml_str(root.path)}",
            f"exclude = {toml_list(root.exclude)}",
        ]
    return "\n".join(lines) + "\n"


def _parse(path: Path, data: Mapping[str, Any]) -> KeepConfig:
    reader = _Reader(path)
    reader.warn_unknown(data, {"keep", "theme", "roots"}, "top level")

    keep = reader.table(data, "keep")
    reader.warn_unknown(keep, {"id", "name", "format_version"}, "[keep]")
    raw_id = reader.string(keep, "id", "[keep]")
    try:
        keep_id = uuid.UUID(raw_id)
    except ValueError as e:
        raise KeepConfigError(path, f"[keep] id is not a UUID: {raw_id!r}") from e
    name = reader.string(keep, "name", "[keep]")
    format_version = reader.integer(keep, "format_version", "[keep]")

    theme_table = reader.table(data, "theme")
    reader.warn_unknown(theme_table, {"id", "version"}, "[theme]")
    theme = ThemeRef(
        id=reader.string(theme_table, "id", "[theme]"),
        version=reader.integer(theme_table, "version", "[theme]"),
    )

    raw_roots = data.get("roots", [])
    if not isinstance(raw_roots, list) or not all(isinstance(r, dict) for r in raw_roots):
        raise KeepConfigError(path, "roots must be written as [[roots]] tables")
    roots: list[RootConfig] = []
    for i, raw in enumerate(raw_roots, start=1):
        where = f"[[roots]] #{i}"
        reader.warn_unknown(raw, {"id", "name", "path", "exclude"}, where)
        exclude = raw.get("exclude", [])
        if not isinstance(exclude, list) or not all(isinstance(p, str) for p in exclude):
            raise KeepConfigError(path, f"{where} exclude must be a list of strings")
        roots.append(
            RootConfig(
                id=reader.string(raw, "id", where),
                name=reader.string(raw, "name", where),
                path=reader.string(raw, "path", where),
                exclude=list(exclude),
            )
        )
    seen: set[str] = set()
    for root in roots:
        if root.id in seen:
            raise KeepConfigError(path, f"duplicate root id {root.id!r}")
        seen.add(root.id)

    return KeepConfig(
        id=keep_id, name=name, theme=theme, format_version=format_version, roots=roots
    )


class _Reader:
    """Typed accessors that raise :class:`KeepConfigError` with the file path and location."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def table(self, data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        value = data.get(key)
        if not isinstance(value, dict):
            raise KeepConfigError(self.path, f"missing [{key}] table")
        return value

    def string(self, data: Mapping[str, Any], key: str, where: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise KeepConfigError(self.path, f"{where} {key} must be a non-empty string")
        return value

    def integer(self, data: Mapping[str, Any], key: str, where: str) -> int:
        value = data.get(key)
        # bool is a subclass of int; `version = true` is a mistake, not 1.
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise KeepConfigError(self.path, f"{where} {key} must be a positive integer")
        return value

    def warn_unknown(self, data: Mapping[str, Any], known: set[str], where: str) -> None:
        for key in sorted(set(data) - known):
            logger.warning("%s: ignoring unknown key %r in %s", self.path, key, where)
