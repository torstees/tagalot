"""Icons for types and keeps (#556, DESIGN.md §12 *Icons*): which picture an icon name
means. Drawing them is the UI's job (``ui/icons.py``).

An icon is named by a theme as one of the bundled `Lucide <https://lucide.dev>`_ icons
(``"film"``), or as an SVG file beside the theme's module (``"icons/wand.svg"``). Either
way it resolves to a *source*: the Lucide name, or the SVG's absolute path, which the UI
turns into a picture drawn in the palette's text color.
"""

import functools
import sys
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath

from tagalot.themes.api import Entity, Theme

GENERIC = "file"
"""The Lucide icon shown for a type without one, or whose icon can't be found."""


def lucide_folder() -> Traversable:
    """The bundled Lucide icons (``scripts/fetch_lucide.py``)."""
    return resources.files("tagalot.resources") / "icons"


@functools.cache
def lucide_names() -> frozenset[str]:
    """The names of the bundled Lucide icons."""
    return frozenset(
        f.name.removesuffix(".svg") for f in lucide_folder().iterdir() if f.name.endswith(".svg")
    )


def lucide_svg(name: str) -> bytes | None:
    """A bundled Lucide icon's SVG, or None for a name not bundled."""
    if name not in lucide_names():
        return None
    return (lucide_folder() / f"{name}.svg").read_bytes()


def theme_folder(theme: type[Theme]) -> Path | None:
    """The folder of the theme's module (a package's own folder), where its SVG icons
    are; None when it can't be told."""
    module = sys.modules.get(theme.__module__)
    file = getattr(module, "__file__", None)
    return Path(file).parent if file else None


def is_svg_path(value: str) -> bool:
    return value.lower().endswith(".svg")


def resolve_icon(value: object, folder: Path | None) -> str | None:
    """The source an icon name means: a bundled Lucide name, or the absolute path of an SVG
    in ``folder`` (the theme's), as a string. None for no icon, or one not found."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if not is_svg_path(value):
        return value if value in lucide_names() else None
    relative = PurePosixPath(value.replace("\\", "/"))
    if folder is None or relative.is_absolute() or ".." in relative.parts:
        return None
    path = folder.joinpath(*relative.parts)
    return str(path) if path.is_file() else None


def type_icon(theme: type[Theme], entity: type[Entity]) -> str | None:
    """The source of a type's icon (:attr:`Entity.icon`), or None when it has none."""
    return resolve_icon(getattr(entity, "icon", None), theme_folder(theme))


def keep_icon(theme: type[Theme]) -> str | None:
    """The source of the theme's icon for its keeps (:attr:`Theme.icon`), or None."""
    return resolve_icon(getattr(theme, "icon", None), theme_folder(theme))


def icon_warnings(theme: type[Theme]) -> list[str]:
    """Icons the theme names that can't be found (``--check-theme`` reports them; they
    show the generic icon)."""
    folder = theme_folder(theme)
    named = [(f"{theme.__name__}.icon", getattr(theme, "icon", None))]
    named += [(f"{e.__name__}.icon", getattr(e, "icon", None)) for e in theme.entities]
    warnings = []
    for where, value in named:
        if value is None or resolve_icon(value, folder) is not None:
            continue
        if isinstance(value, str) and is_svg_path(value):
            warnings.append(
                f"{where}: {value!r} isn't an SVG file in the theme's folder; "
                "the generic icon shows instead"
            )
        else:
            warnings.append(
                f"{where}: {value!r} isn't one of the Lucide icons Tagalot includes (THEMES.md "
                "lists them); the generic icon shows instead"
            )
    return warnings
