"""Keep-wide settings edited in the Keep configuration window (DESIGN.md §4, §12).

Each function returns a changed copy of the :class:`~tagalot.core.keep.KeepConfig`, checked
before anything is written (the session saves it), or raises
:class:`~tagalot.core.keep.KeepError` with a message for the user: the keep's name, the
thumbnail size override, and theme option values for the keep or one root.
"""

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

from tagalot.core.keep import KeepConfig, KeepError, validate_keep_config
from tagalot.themes.api import Theme, ThemeOption
from tagalot.themes.loader import MAX_THUMBNAIL_SIZE

MIN_THUMBNAIL_SIZE = 16


def with_name(config: KeepConfig, keep_dir: Path, name: str) -> KeepConfig:
    """``config`` with the keep renamed."""
    if not name.strip():
        raise KeepError("A keep needs a name.")
    new = replace(config, name=name.strip(), roots=copy.deepcopy(config.roots))
    validate_keep_config(new, keep_dir / "keep.toml")
    return new


def with_thumbnail_max(config: KeepConfig, size: int | None) -> KeepConfig:
    """``config`` making thumbnails at ``size`` pixels (``None``: the theme's size)."""
    if size is not None and not MIN_THUMBNAIL_SIZE <= size <= MAX_THUMBNAIL_SIZE:
        raise KeepError(
            f"Thumbnails can be {MIN_THUMBNAIL_SIZE} to {MAX_THUMBNAIL_SIZE} pixels, not {size}."
        )
    return replace(config, thumbnail_max=size, roots=copy.deepcopy(config.roots))


def with_option(
    config: KeepConfig,
    theme: type[Theme],
    name: str,
    value: Any,
    *,
    root_id: str | None = None,
) -> KeepConfig:
    """``config`` with a theme option set for the keep, or for one root (``root_id``).
    ``None`` removes the setting: the keep's then use the default, a root's the keep's."""
    spec = option_spec(theme, name)
    if value is not None:
        value = checked_value(spec, value)
    roots = copy.deepcopy(config.roots)
    if root_id is None:
        options = dict(config.theme_options)
        _set(options, name, value)
        return replace(config, theme_options=options, roots=roots)
    root = next((r for r in roots if r.id == root_id), None)
    if root is None:
        raise KeepError(f"This keep has no root {root_id!r}.")
    _set(root.options, name, value)
    return replace(config, roots=roots)


def option_spec(theme: type[Theme], name: str) -> ThemeOption:
    spec = next((o for o in theme.options if o.name == name), None)
    if spec is None:
        raise KeepError(f"The {theme.name} theme has no option {name!r}.")
    return spec


def checked_value(spec: ThemeOption, value: Any) -> Any:
    """``value`` as the option's type, or :class:`KeepError` saying what it needs."""
    if spec.type is bool:
        if isinstance(value, bool):
            return value
    elif spec.type is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif spec.type is float:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    elif isinstance(value, str):
        return value
    kinds = {bool: "true or false", int: "a whole number", float: "a number", str: "text"}
    raise KeepError(f"{spec.label} must be {kinds.get(spec.type, spec.type.__name__)}.")


def _set(options: dict[str, Any], name: str, value: Any) -> None:
    if value is None:
        options.pop(name, None)
    else:
        options[name] = value
