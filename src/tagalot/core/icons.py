"""Icons for types and keeps (#556, DESIGN.md §12 *Icons*): which picture an icon name
means. Drawing them is the UI's job (``ui/icons.py``).

An icon is named by a theme as one of the bundled `Lucide <https://lucide.dev>`_ icons
(``"film"``), or as an SVG file beside the theme's module (``"icons/wand.svg"``). Either
way it resolves to a *source*: the Lucide name, or the SVG's absolute path, which the UI
turns into a picture drawn in the palette's text color.

The user can choose others (#557): for a type and for the keep in ``keep.toml``
(``[appearance]``), and for a saved search in its definition. Theirs is a Lucide name
too, or a picture (SVG or PNG) copied into the keep folder's ``icons/`` folder, so the
keep still shows it from another computer; nothing is written anywhere else.
"""

import functools
import hashlib
import json
import sys
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath

from tagalot.core.keep import Keep
from tagalot.themes.api import Entity, Theme

GENERIC = "file"
"""The Lucide icon shown for a type without one, or whose icon can't be found."""
KEEP_ICONS = "icons"
"""The keep folder's folder for pictures the user chose as icons."""
PICTURE_EXTENSIONS = (".svg", ".png")
"""Pictures the user can choose as an icon (a theme's are SVGs)."""
MAX_PICTURE = 1024 * 1024
"""The largest picture taken as an icon, in bytes."""


def lucide_folder() -> Traversable:
    """The bundled Lucide icons (``scripts/fetch_lucide.py``)."""
    return resources.files("tagalot.resources") / "icons"


@functools.cache
def lucide_names() -> frozenset[str]:
    """The names of the bundled Lucide icons."""
    return frozenset(
        f.name.removesuffix(".svg") for f in lucide_folder().iterdir() if f.name.endswith(".svg")
    )


@functools.cache
def lucide_tags() -> dict[str, tuple[str, ...]]:
    """Each bundled Lucide icon's keywords (``tags.json``): ``film`` is a movie, a reel,
    cinema… For searching the icon picker."""
    data = json.loads((lucide_folder() / "tags.json").read_text(encoding="utf-8"))
    return {name: tuple(data.get(name, ())) for name in lucide_names()}


def matching_icons(text: str) -> list[str]:
    """The bundled Lucide icons whose name or keywords contain every word of ``text``, by
    name; all of them for no text."""
    words = text.casefold().split()
    tags = lucide_tags()
    return sorted(
        name
        for name in lucide_names()
        if all(w in name or any(w in tag for tag in tags[name]) for w in words)
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


def is_picture_path(value: str) -> bool:
    return value.lower().endswith(PICTURE_EXTENSIONS)


def resolve_icon(
    value: object, folder: Path | None, extensions: tuple[str, ...] = (".svg",)
) -> str | None:
    """The source an icon name means: a bundled Lucide name, or the absolute path of a
    picture with one of ``extensions`` in ``folder`` (the theme's, or the keep's), as a
    string. None for no icon, or one not found."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if not value.lower().endswith(extensions):
        return value if value in lucide_names() else None
    relative = PurePosixPath(value.replace("\\", "/"))
    if folder is None or relative.is_absolute() or ".." in relative.parts:
        return None
    path = folder.joinpath(*relative.parts)
    return str(path) if path.is_file() else None


def user_icon(value: object, keep: Keep) -> str | None:
    """The source of an icon the user chose in ``keep`` (a Lucide name, or a picture in
    the keep folder), or None when it can't be found."""
    return resolve_icon(value, keep.dir, PICTURE_EXTENSIONS)


def type_icon(theme: type[Theme], entity: type[Entity], keep: Keep | None = None) -> str | None:
    """The source of a type's icon: the one the user chose in ``keep``
    (``[appearance.icons]``), else the theme's (:attr:`Entity.icon`); None when it has
    neither."""
    if keep is not None:
        own = user_icon(keep.config.type_icons.get(theme.type_id_of(entity)), keep)
        if own is not None:
            return own
    return resolve_icon(getattr(entity, "icon", None), theme_folder(theme))


def keep_icon(theme: type[Theme], keep: Keep | None = None) -> str | None:
    """The source of a keep's icon: the one the user chose (``[appearance] icon``), else
    its theme's (:attr:`Theme.icon`); None when it has neither."""
    if keep is not None:
        own = user_icon(keep.config.icon, keep)
        if own is not None:
            return own
    return resolve_icon(getattr(theme, "icon", None), theme_folder(theme))


class IconFileError(ValueError):
    """A picture that can't be an icon; the message is for the user."""


def check_user_icon(value: str) -> str:
    """``value`` as stored for the user's icon: a bundled Lucide name, or a picture's
    path in the keep folder (``icons/…``). Raises :class:`IconFileError` for anything
    else."""
    value = value.strip()
    if value in lucide_names():
        return value
    relative = PurePosixPath(value.replace("\\", "/"))
    if (
        len(relative.parts) == 2
        and relative.parts[0] == KEEP_ICONS
        and is_picture_path(relative.name)
    ):
        return relative.as_posix()
    raise IconFileError(f"{value!r} isn't a Lucide icon or a picture in the keep's icons.")


def copy_icon_picture(keep_dir: Path, source: Path) -> str:
    """Copy a picture the user chose as an icon into the keep folder's ``icons/`` (the
    same picture is copied once; another with the same name gets a number) and return
    its path there, as stored (``icons/wand.svg``). Raises :class:`IconFileError`. Does
    I/O: call it from a worker."""
    if not is_picture_path(source.name):
        raise IconFileError("Choose an SVG or PNG picture.")
    try:
        data = source.read_bytes()
    except OSError as e:
        raise IconFileError(f"Couldn't read {source.name}: {e.strerror or e}") from None
    if len(data) > MAX_PICTURE:
        raise IconFileError(f"{source.name} is too big for an icon (1 MB at most).")
    folder = keep_dir / KEEP_ICONS
    folder.mkdir(exist_ok=True)
    stem, suffix = source.stem.strip() or "icon", source.suffix.lower()
    digest = hashlib.blake2b(data).digest()
    for n in range(1, 1000):
        name = f"{stem}{suffix}" if n == 1 else f"{stem} {n}{suffix}"
        target = folder / name
        if target.is_file() and hashlib.blake2b(target.read_bytes()).digest() == digest:
            return f"{KEEP_ICONS}/{name}"  # already copied
        if not target.exists():
            partial = target.with_name(target.name + ".part")
            partial.write_bytes(data)
            partial.replace(target)
            return f"{KEEP_ICONS}/{name}"
    raise IconFileError(f"Too many icons named {source.name} in the keep.")


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
