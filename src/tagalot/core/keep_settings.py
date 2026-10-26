"""Keep-wide settings edited in the Keep configuration window (DESIGN.md §4, §12).

Each function returns a changed copy of the :class:`~tagalot.core.keep.KeepConfig`, checked
before anything is written (the session saves it), or raises
:class:`~tagalot.core.keep.KeepError` with a message for the user: the keep's name, the
thumbnail size override, and theme option values for the keep or one root.
"""

import copy
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from tagalot.core.assets import clean_setting
from tagalot.core.keep import (
    CONTENTS_INDEXES,
    FIND_BY,
    ONLINE_ANSWERS,
    KeepConfig,
    KeepError,
    allowed_hosts,
    clean_names,
    validate_keep_config,
)
from tagalot.core.note_keys import NoteKeyError, check_alias, check_key
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


def with_thumbnails_after_scan(config: KeepConfig, on: bool) -> KeepConfig:
    """``config`` making (or not) a scan's thumbnails in the background."""
    return replace(config, thumbnails_after_scan=on, roots=copy.deepcopy(config.roots))


def with_online_lookups(
    config: KeepConfig, answer: str | None, hosts: Iterable[str] = ()
) -> KeepConfig:
    """``config`` allowing (``"allow"``) online lookups from the services on ``hosts``, or
    refusing (``"never"``) them all, or not yet asked (``None``)."""
    if answer not in (None, *ONLINE_ANSWERS):
        raise KeepError(f"Online lookups can be allowed or never, not {answer!r}.")
    allowed = sorted(set(hosts)) if answer == "allow" else None
    return replace(
        config,
        online_lookups=answer,
        online_allowed=allowed,
        online_refused=[],
        roots=copy.deepcopy(config.roots),
    )


def with_online_services(
    config: KeepConfig,
    *,
    allow: Iterable[str] = (),
    refuse: Iterable[str] = (),
    theme_hosts: Iterable[str] = (),
) -> KeepConfig:
    """``config`` (which allows lookups) also allowing the services on ``allow``, and not
    using those on ``refuse`` (#483). Of the services an older keep allowed without listing
    them, only the theme's (``theme_hosts``) are listed."""
    if config.online_lookups != "allow":
        raise KeepError("Online lookups aren't allowed for this keep.")
    before = allowed_hosts(config)
    if config.online_allowed is None:
        before &= set(theme_hosts)
    allowed = (before | set(allow)) - set(refuse)
    refused = (set(config.online_refused) | set(refuse)) - set(allow)
    return replace(
        config,
        online_allowed=sorted(allowed),
        online_refused=sorted(refused),
        roots=copy.deepcopy(config.roots),
    )


def with_online_find_by(config: KeepConfig, find_by: str) -> KeepConfig:
    """``config`` letting items be identified to services by ``find_by`` (#485):
    ``"ids"``, ``"title-year"``, or ``"title"``."""
    if find_by not in FIND_BY:
        raise KeepError(
            f"Items can be found by IDs, a title with its year, or a title, not {find_by!r}."
        )
    return replace(config, online_find_by=find_by, roots=copy.deepcopy(config.roots))


def with_contents_index(config: KeepConfig, index: str | None) -> KeepConfig:
    """``config`` searching inside documents with a Words (``"words"``) or Substrings
    (``"substrings"``) index, or not at all (``None``)."""
    if index not in (None, *CONTENTS_INDEXES):
        raise KeepError(f"Contents search can be words or substrings, not {index!r}.")
    return replace(config, contents_index=index, roots=copy.deepcopy(config.roots))


def with_markdown_assets(config: KeepConfig, assets: str) -> KeepConfig:
    """``config`` with the keep's default assets folder (``[markdown] assets``, #418);
    ``""`` for none."""
    try:
        cleaned = clean_setting(assets)
    except ValueError as e:
        raise KeepError(str(e)) from None
    return replace(config, markdown_assets=cleaned, roots=copy.deepcopy(config.roots))


def with_see_through(config: KeepConfig, names: Iterable[str]) -> KeepConfig:
    """``config`` with these see-through folder names (``[folders] see_through``, #459),
    trimmed, each once (case aside), in order."""
    return replace(config, see_through=clean_names(names), roots=copy.deepcopy(config.roots))


def with_note_ignore_keys(config: KeepConfig, keys: Iterable[str]) -> KeepConfig:
    """``config`` with these front-matter keys ignored in its notes (``[notes]
    ignore_keys``), each once (case aside), in order; ``tagalot-…`` keys are refused."""
    listed: list[str] = []
    for key in keys:
        try:
            key = check_key(key)
        except NoteKeyError as e:
            raise KeepError(str(e)) from None
        if key.casefold() not in {k.casefold() for k in listed}:
            listed.append(key)
    return replace(config, note_ignore_keys=listed, roots=copy.deepcopy(config.roots))


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


def with_note_key_aliases(config: KeepConfig, aliases: Mapping[str, str]) -> KeepConfig:
    """``config`` with these front-matter key aliases (``[notes.key_aliases]``, #411): key ->
    a field's name or label, each key once (case aside, the last winning), in order; keys
    Tagalot reads itself are refused."""
    listed: dict[str, str] = {}
    for key, field_name in aliases.items():
        try:
            key, field_name = check_alias(key, field_name)
        except NoteKeyError as e:
            raise KeepError(str(e)) from None
        for known in [k for k in listed if k.casefold() == key.casefold()]:
            del listed[known]
        listed[key] = field_name
    return replace(config, note_key_aliases=listed, roots=copy.deepcopy(config.roots))
