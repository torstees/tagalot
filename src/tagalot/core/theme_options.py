"""Theme options: the values in effect for a root (DESIGN.md §4, §9).

A theme declares options (``Theme.options``); a keep sets them in ``keep.toml`` under
``[theme.options]``, and a root can override them (``options = { … }``). Values that don't
fit the declaration are reported and replaced by the default, so a typo never stops a scan.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tagalot.themes.api import Theme


@dataclass(frozen=True)
class EffectiveOptions:
    """Every declared option's value for one root, and what was wrong with the settings."""

    values: dict[str, Any]
    problems: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """A stable text form, stored per root to notice when the values change."""
        return json.dumps(self.values, sort_keys=True)


def effective_options(
    theme: type[Theme], keep_options: Mapping[str, Any], root_options: Mapping[str, Any]
) -> EffectiveOptions:
    """The options in effect for a root: its override, else the keep's, else the default."""
    declared = {o.name: o for o in theme.options}
    values = {name: o.default for name, o in declared.items()}
    problems: list[str] = []
    for where, settings in (
        ("[theme.options]", keep_options),
        ("the root's options", root_options),
    ):
        for name, value in settings.items():
            spec = declared.get(name)
            if spec is None:
                problems.append(f"{where}: the {theme.name} theme has no option {name!r}")
                continue
            if isinstance(value, bool) != (spec.type is bool):
                ok = False
            elif spec.type is float and isinstance(value, int | float):
                value, ok = float(value), True
            else:
                ok = isinstance(value, spec.type)
            if not ok:
                problems.append(
                    f"{where}: {name} must be {_kind(spec.type)}, not {value!r}; "
                    f"using {values[name]!r}"
                )
                continue
            values[name] = value
    return EffectiveOptions(values, problems)


def _kind(kind: type) -> str:
    return {bool: "true or false", int: "a whole number", float: "a number", str: "text"}[kind]
