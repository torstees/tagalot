"""Per-user, per-machine settings (DESIGN.md §4): ``settings.toml`` in the user config dir.

Settings never block startup. A missing file means defaults; an unreadable file is set aside
as ``settings.toml.invalid`` and defaults are used; individual bad entries are skipped. Every
problem is logged.
"""

import logging
import os
import string
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs

from tagalot.core.keep import RootConfig
from tagalot.core.tomlio import toml_key, toml_list, toml_str, write_atomic

logger = logging.getLogger(__name__)

SETTINGS_TOML = "settings.toml"
MAX_RECENT_KEEPS = 10
COMMAND_PLACEHOLDERS = frozenset({"path", "dir", "name"})
"""Placeholders allowed in handler command templates (DESIGN.md §11)."""


def default_settings_path() -> Path:
    """Return the settings file location, e.g. ``%LOCALAPPDATA%\\tagalot\\settings.toml``.

    This is deliberately not a roaming location: overrides hold machine-specific paths.
    """
    return Path(platformdirs.user_config_dir("tagalot", appauthor=False)) / SETTINGS_TOML


@dataclass
class HandlerOverride:
    """Open files with ``ext`` (and optionally only in ``role``) using a command template."""

    ext: str
    command: str
    role: str | None = None

    def __post_init__(self) -> None:
        self.ext = normalize_ext(self.ext)


@dataclass
class Settings:
    """The contents of ``settings.toml``."""

    recent_keeps: list[Path] = field(default_factory=list)
    """Keep folders, most recent first."""
    root_overrides: dict[uuid.UUID, dict[str, str]] = field(default_factory=dict)
    """Keep id -> root id -> local path, for reaching a share through a different mount."""
    handlers: list[HandlerOverride] = field(default_factory=list)
    theme_dirs: list[Path] = field(default_factory=list)
    last_folder: Path | None = None
    """The folder last chosen in one of Tagalot's folder pickers, where the next one (and a
    new keep's location) starts."""
    """Extra directories searched for themes."""

    def add_recent_keep(self, keep_dir: Path) -> None:
        """Move ``keep_dir`` to the front of the recent list, dropping duplicates and extras."""
        key = _path_key(keep_dir)
        others = [p for p in self.recent_keeps if _path_key(p) != key]
        self.recent_keeps = [keep_dir, *others][:MAX_RECENT_KEEPS]

    def remove_recent_keep(self, keep_dir: Path) -> None:
        """Remove ``keep_dir`` from the recent list if present."""
        key = _path_key(keep_dir)
        self.recent_keeps = [p for p in self.recent_keeps if _path_key(p) != key]

    def root_path(self, keep_id: uuid.UUID, root: RootConfig) -> str:
        """Return the path to use for ``root`` on this machine: the override, else keep.toml's."""
        return self.root_overrides.get(keep_id, {}).get(root.id, root.path)

    def set_root_override(self, keep_id: uuid.UUID, root_id: str, path: str | None) -> None:
        """Set this machine's path for a root, or clear it with ``None``."""
        overrides = self.root_overrides.setdefault(keep_id, {})
        if path is None:
            overrides.pop(root_id, None)
            if not overrides:
                del self.root_overrides[keep_id]
        else:
            overrides[root_id] = path

    def set_handler(self, ext: str, command: str, role: str | None = None) -> HandlerOverride:
        """Open files with ``ext`` (in ``role``, if given) with ``command``, replacing the rule
        for the same extension and role."""
        handler = HandlerOverride(ext=ext, command=command, role=role)
        self.handlers = [
            h for h in self.handlers if (h.ext, h.role) != (handler.ext, handler.role)
        ] + [handler]
        return handler

    def remove_handler(self, handler: HandlerOverride) -> None:
        """Forget a rule (the one :meth:`handler_for` returned, say)."""
        self.handlers = [h for h in self.handlers if (h.ext, h.role) != (handler.ext, handler.role)]

    def handler_for(self, ext: str, role: str | None = None) -> HandlerOverride | None:
        """Return the override for a file: a rule for this extension and role wins over one for
        the extension alone. Extensions match case-insensitively."""
        ext = normalize_ext(ext)
        general: HandlerOverride | None = None
        for handler in self.handlers:
            if handler.ext != ext:
                continue
            if role is not None and handler.role == role:
                return handler
            if handler.role is None and general is None:
                general = handler
        return general


def normalize_ext(ext: str) -> str:
    """Return ``ext`` lowercased with a leading dot: ``"PSD"`` -> ``".psd"``."""
    ext = ext.strip().lower()
    return ext if ext.startswith(".") else f".{ext}"


def command_placeholder_error(command: str) -> str | None:
    """Return why ``command`` is not a valid handler template, or ``None`` if it is."""
    if not command.strip():
        return "command is empty"
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(command) if f is not None]
    except ValueError as e:
        return f"command has unbalanced braces ({e}); write literal braces as {{{{ and }}}}"
    unknown = sorted(set(fields) - COMMAND_PLACEHOLDERS)
    if unknown:
        allowed = ", ".join(f"{{{p}}}" for p in sorted(COMMAND_PLACEHOLDERS))
        return f"unknown placeholder(s) {', '.join(unknown)}; use {allowed}"
    return None


def load_settings(path: Path | None = None) -> Settings:
    """Read settings, falling back to defaults on any problem (see the module docstring)."""
    path = path or default_settings_path()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return Settings()
    except (OSError, tomllib.TOMLDecodeError) as e:
        _set_aside(path, e)
        return Settings()
    return _parse(path, data)


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Write settings atomically, creating the config directory if needed."""
    path = path or default_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, dump_settings(settings))


def dump_settings(settings: Settings) -> str:
    """Serialize settings as hand-editable TOML."""
    lines = [
        "# Tagalot per-user settings. Paths here are specific to this machine.",
        "",
        f"recent_keeps = {toml_list(str(p) for p in settings.recent_keeps)}",
        f"theme_dirs = {toml_list(str(p) for p in settings.theme_dirs)}",
    ]
    if settings.last_folder is not None:
        lines.append(f"last_folder = {toml_str(str(settings.last_folder))}")
    for keep_id, overrides in settings.root_overrides.items():
        if not overrides:
            continue
        lines += ["", f"[root_overrides.{toml_key(str(keep_id))}]"]
        lines += [f"{toml_key(root_id)} = {toml_str(p)}" for root_id, p in overrides.items()]
    for handler in settings.handlers:
        lines += ["", "[[handlers]]", f"ext = {toml_str(handler.ext)}"]
        if handler.role is not None:
            lines.append(f"role = {toml_str(handler.role)}")
        lines.append(f"command = {toml_str(handler.command)}")
    return "\n".join(lines) + "\n"


def _path_key(path: Path) -> str:
    """Compare keep folders the way the OS does (case-insensitive on Windows)."""
    return os.path.normcase(os.path.abspath(path))


def _set_aside(path: Path, error: Exception) -> None:
    backup = path.with_name(path.name + ".invalid")
    try:
        path.replace(backup)
    except OSError as e:
        logger.warning(
            "Cannot read settings %s (%s); also failed to set it aside: %s", path, error, e
        )
        return
    logger.warning(
        "Cannot read settings %s (%s); moved it to %s and using defaults", path, error, backup
    )


def _string_list(path: Path, data: Mapping[str, Any], key: str) -> list[str]:
    value = data.get(key, [])
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    logger.warning("%s: %s must be a list of strings; ignoring it", path, key)
    return []


def _parse(path: Path, data: Mapping[str, Any]) -> Settings:
    known = {"recent_keeps", "theme_dirs", "last_folder", "root_overrides", "handlers"}
    for key in sorted(set(data) - known):
        logger.warning("%s: ignoring unknown key %r", path, key)

    settings = Settings(
        recent_keeps=[Path(p) for p in _string_list(path, data, "recent_keeps")][:MAX_RECENT_KEEPS],
        theme_dirs=[Path(p) for p in _string_list(path, data, "theme_dirs")],
    )
    last_folder = data.get("last_folder")
    if isinstance(last_folder, str) and last_folder.strip():
        settings.last_folder = Path(last_folder)
    elif last_folder is not None:
        logger.warning("%s: last_folder must be a path; ignoring it", path)

    raw_overrides = data.get("root_overrides", {})
    if not isinstance(raw_overrides, dict):
        logger.warning("%s: root_overrides must be a table; ignoring it", path)
        raw_overrides = {}
    for raw_keep_id, roots in raw_overrides.items():
        try:
            keep_id = uuid.UUID(raw_keep_id)
        except ValueError:
            logger.warning(
                "%s: root_overrides key %r is not a keep id; ignoring it", path, raw_keep_id
            )
            continue
        if not isinstance(roots, dict):
            logger.warning(
                "%s: [root_overrides.%s] must be a table; ignoring it", path, raw_keep_id
            )
            continue
        for root_id, local in roots.items():
            if isinstance(local, str) and local.strip():
                settings.set_root_override(keep_id, root_id, local)
            else:
                logger.warning(
                    "%s: override for root %r in keep %s must be a path; ignoring it",
                    path,
                    root_id,
                    keep_id,
                )

    raw_handlers = data.get("handlers", [])
    if not isinstance(raw_handlers, list):
        logger.warning("%s: handlers must be written as [[handlers]] tables; ignoring them", path)
        raw_handlers = []
    for i, raw in enumerate(raw_handlers, start=1):
        handler = _parse_handler(raw)
        if isinstance(handler, str):
            logger.warning("%s: [[handlers]] #%d %s; ignoring it", path, i, handler)
        else:
            settings.handlers.append(handler)
    return settings


def _parse_handler(raw: object) -> HandlerOverride | str:
    """Return a handler, or a description of what is wrong with it."""
    if not isinstance(raw, dict):
        return "is not a table"
    ext, command, role = raw.get("ext"), raw.get("command"), raw.get("role")
    if not isinstance(ext, str) or not ext.strip(".").strip():
        return "needs an ext such as '.psd'"
    if not isinstance(command, str):
        return "needs a command"
    if role is not None and (not isinstance(role, str) or not role.strip()):
        return "role must be a non-empty string"
    if error := command_placeholder_error(command):
        return error
    return HandlerOverride(ext=ext, command=command, role=role)
