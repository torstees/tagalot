"""Color schemes (#572, DESIGN.md §12 *Colors*): named sets of a keep's five colors.

A keep's ``[appearance] colors`` names its look:

- ``"system"``, ``"light"``, ``"dark"``: the system's own look (:data:`NATIVE`), not schemes;
- a built-in scheme's id (``"nord"``, :data:`BUILT_IN`);
- a theme's scheme, by the theme's id and the scheme's (``"books.library"``,
  ``Theme.color_schemes``), offered only in that theme's keeps;
- the user's own, ``"user:"`` and its file's name (``"user:my-parchment"``): one TOML file
  per scheme in the per-user data folder's ``color_schemes`` (:func:`default_schemes_dir`),
  offered in every keep;
- ``"custom"``: colors written in ``keep.toml`` itself, as #558 saved them, still read.

The user's schemes are read once and kept (:func:`user_schemes`), so a window becoming
active never reads a file; saving one updates them. A file edited by hand is read the next
time Tagalot starts.
"""

import logging
import re
import threading
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import platformdirs

from tagalot.core.keep import COLOR_ROLES, COLOR_SCHEME_ID, KeepConfig
from tagalot.core.tomlio import toml_str, write_atomic
from tagalot.themes.api import ColorScheme, Theme

logger = logging.getLogger(__name__)

NATIVE = ("system", "light", "dark")
"""The looks that aren't schemes: the system's own, following its mode or always light or
dark."""
CUSTOM = "custom"
"""A keep's own colors written in ``keep.toml`` (#558), shown until saved as a scheme."""
USER = "user:"
"""The prefix of the user's own schemes' ids."""
READABLE_TEXT = 4.5
"""The contrast below which a scheme's text is said to be hard to read (WCAG AA)."""

_ID = r"[a-z0-9][a-z0-9_-]*"
_HEX = re.compile(r"#[0-9a-f]{6}")


BUILT_IN: tuple[ColorScheme, ...] = (
    # id, name, background, text, panels, accent, dimmed
    ColorScheme("paper", "Paper", "#f4f4f2", "#1d1d1f", "#ffffff", "#2f6fbf", "#6e6e73"),
    ColorScheme("parchment", "Parchment", "#f3ece0", "#2b2722", "#fffaf2", "#8a5a2b", "#7a6f61"),
    ColorScheme(
        "solarized-light", "Solarized Light", "#fdf6e3", "#4a5e66", "#eee8d5", "#268bd2", "#7b8c8c"
    ),
    ColorScheme("slate", "Slate", "#23272e", "#e6e8eb", "#1b1e23", "#5b8def", "#9097a1"),
    ColorScheme("nord", "Nord", "#2e3440", "#eceff4", "#3b4252", "#88c0d0", "#a3abbd"),
    ColorScheme(
        "solarized-dark", "Solarized Dark", "#002b36", "#a9b7b7", "#073642", "#268bd2", "#7c9298"
    ),
)
"""The schemes Tagalot comes with."""


class ColorSchemeError(Exception):
    """A scheme that can't be saved, with a message for the user."""


def default_schemes_dir() -> Path:
    """The user's own schemes' folder, beside the user's themes folder."""
    return Path(platformdirs.user_data_dir("tagalot", appauthor=False)) / "color_schemes"


def is_scheme_id(value: str) -> bool:
    """Whether ``value`` can name a keep's look in ``[appearance] colors``."""
    return value in NATIVE or value == CUSTOM or COLOR_SCHEME_ID.fullmatch(value) is not None


def scheme_colors(scheme: ColorScheme) -> dict[str, str]:
    """A scheme's colors by role name (``COLOR_ROLES``)."""
    return {role: getattr(scheme, role) for role in COLOR_ROLES}


def check_colors(colors: Mapping[str, str]) -> dict[str, str]:
    """All five colors, cleaned (``#rrggbb`` in lowercase); raises ``ColorSchemeError``."""
    cleaned = {}
    for role in COLOR_ROLES:
        value = str(colors.get(role, "")).strip().lower()
        if not _HEX.fullmatch(value):
            raise ColorSchemeError(f"The {role} color must look like #1f1d1a, not {value!r}.")
        cleaned[role] = value
    return cleaned


def theme_schemes(theme: type[Theme] | None) -> list[ColorScheme]:
    """A theme's schemes, with the ids keeps name them by (``"books.library"``)."""
    if theme is None:
        return []
    return [replace(s, id=f"{theme.id}.{s.id}") for s in getattr(theme, "color_schemes", ())]


# --- the user's own schemes ---

_lock = threading.Lock()
_loaded: dict[Path, list[ColorScheme]] = {}


def user_schemes(folder: Path | None = None) -> list[ColorScheme]:
    """The user's own schemes, by name; read once, then kept. Files that can't be read are
    left out, with a warning in the log."""
    folder = folder or default_schemes_dir()
    with _lock:
        if folder not in _loaded:
            _loaded[folder] = _read_schemes(folder)
        return list(_loaded[folder])


def _read_schemes(folder: Path) -> list[ColorScheme]:
    schemes = []
    try:
        files = sorted(folder.glob("*.toml"))
    except OSError as e:
        logger.warning("Can't list the color schemes in %s: %s", folder, e)
        return []
    for path in files:
        scheme = read_scheme(path)
        if scheme is not None:
            schemes.append(scheme)
    return sorted(schemes, key=lambda s: s.name.casefold())


def read_scheme(path: Path) -> ColorScheme | None:
    """One of the user's scheme files, or None (with a warning) when it can't be used."""
    slug = path.stem.lower()
    if not re.fullmatch(_ID, slug):
        logger.warning("Ignoring the color scheme %s: its file name isn't a usable id", path)
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        name = data.get("name", slug)
        if not isinstance(name, str) or not name.strip():
            raise ColorSchemeError("its name must be a text")
        colors = check_colors(data)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ColorSchemeError) as e:
        logger.warning("Ignoring the color scheme %s: %s", path, e)
        return None
    return ColorScheme(f"{USER}{slug}", name.strip(), **colors)


def slug_of(name: str) -> str:
    """A scheme's file name (and id) from its name: ``"My Parchment"`` → ``my-parchment``."""
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def name_problem(name: str, folder: Path | None = None) -> str | None:
    """Why ``name`` can't name a new scheme of the user's (empty, no letters, or taken), or
    None when it can."""
    name = name.strip()
    if not name:
        return "Give the scheme a name."
    slug = slug_of(name)
    if not re.fullmatch(_ID, slug):
        return "Give the scheme a name with a letter or digit in it."
    if any(
        s.id == f"{USER}{slug}" or s.name.casefold() == name.casefold()
        for s in user_schemes(folder)
    ):
        return f"You already have a scheme named {name!r}."
    return None


def save_user_scheme(
    name: str,
    colors: Mapping[str, str],
    *,
    scheme_id: str | None = None,
    folder: Path | None = None,
) -> ColorScheme:
    """Write one of the user's schemes: ``scheme_id`` (``"user:…"``) saves over that one
    (**Save**), else a new one is made from ``name`` (**Save As**), refused when the user
    already has a scheme by that name. Returns it. Raises ``ColorSchemeError``."""
    folder = folder or default_schemes_dir()
    name = name.strip()
    if not name:
        raise ColorSchemeError("Give the scheme a name.")
    cleaned = check_colors(colors)
    if scheme_id is not None:
        if not scheme_id.startswith(USER):
            raise ColorSchemeError("Only your own schemes can be saved; use Save As.")
        slug = scheme_id.removeprefix(USER)
    else:
        problem = name_problem(name, folder)
        if problem is not None:
            raise ColorSchemeError(problem)
        slug = slug_of(name)
        if (folder / f"{slug}.toml").exists():
            raise ColorSchemeError(f"You already have a scheme named {name!r}.")
    lines = [f"name = {toml_str(name)}", *(f"{r} = {toml_str(cleaned[r])}" for r in COLOR_ROLES)]
    path = folder / f"{slug}.toml"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        write_atomic(path, "\n".join(lines) + "\n")
    except OSError as e:
        raise ColorSchemeError(f"Can't save the scheme in {folder}: {e}") from e
    scheme = ColorScheme(f"{USER}{slug}", name, **cleaned)
    with _lock:
        known = [s for s in _loaded.get(folder, _read_schemes(folder)) if s.id != scheme.id]
        _loaded[folder] = sorted([*known, scheme], key=lambda s: s.name.casefold())
    logger.info("Saved the color scheme %r in %s", name, path)
    return scheme


# --- choosing ---


def all_schemes(theme: type[Theme] | None, folder: Path | None = None) -> list[ColorScheme]:
    """The schemes a keep can choose from: built in, its theme's, the user's."""
    return [*BUILT_IN, *theme_schemes(theme), *user_schemes(folder)]


def find_scheme(
    scheme_id: str, theme: type[Theme] | None, folder: Path | None = None
) -> ColorScheme | None:
    """The scheme a keep names, or None when there's none by that id."""
    return next((s for s in all_schemes(theme, folder) if s.id == scheme_id), None)


def resolve_colors(
    config: KeepConfig, theme: type[Theme] | None, folder: Path | None = None
) -> tuple[str, dict[str, str]]:
    """The look a keep's configuration names: ``(mode, colors)``, a mode of :data:`NATIVE`
    with no colors, or ``"custom"`` with all five. A scheme that can't be found gives the
    system's look, with a warning in the log."""
    if config.colors in NATIVE:
        return config.colors, {}
    if config.colors == CUSTOM:
        return CUSTOM, dict(config.custom_colors)
    scheme = find_scheme(config.colors, theme, folder)
    if scheme is None:
        logger.warning(
            "Keep %r names the color scheme %r, which isn't there; using the system's colors",
            config.name,
            config.colors,
        )
        return "system", {}
    return CUSTOM, scheme_colors(scheme)


# --- checking ---


def _luminance(color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    """The WCAG contrast ratio of two ``#rrggbb`` colors (1 to 21)."""
    light, dark = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def hard_to_read(colors: Mapping[str, str]) -> list[str]:
    """What reads poorly in a scheme (text against the background and against lists and
    panels, below :data:`READABLE_TEXT`), as sentences."""
    problems = []
    for ground, label in (("background", "the background"), ("panels", "lists and panels")):
        if "text" in colors and ground in colors:
            ratio = contrast(colors["text"], colors[ground])
            if ratio < READABLE_TEXT:
                problems.append(
                    f"Text on {label} is hard to read (contrast {ratio:.1f}:1; "
                    f"{READABLE_TEXT:g}:1 or more reads easily)."
                )
    return problems


def scheme_problems(schemes: Sequence[object]) -> list[str]:
    """What's wrong with a theme's ``color_schemes`` (the loader refuses the theme)."""
    problems = []
    seen: set[str] = set()
    for scheme in schemes:
        if not isinstance(scheme, ColorScheme):
            problems.append("color_schemes must hold ColorScheme values")
            continue
        if not re.fullmatch(_ID, scheme.id):
            problems.append(
                f"color scheme {scheme.id!r}: its id must be lowercase letters, digits, "
                "dashes, or underscores"
            )
        if scheme.id in seen:
            problems.append(f"color scheme {scheme.id!r} is declared twice")
        seen.add(scheme.id)
        if not isinstance(scheme.name, str) or not scheme.name.strip():
            problems.append(f"color scheme {scheme.id!r}: it needs a name")
        for role in COLOR_ROLES:
            value = getattr(scheme, role)
            if not isinstance(value, str) or not _HEX.fullmatch(value.lower()):
                problems.append(
                    f"color scheme {scheme.id!r}: {role} must be a color such as '#1f1d1a'"
                )
    return problems


def scheme_warnings(theme: type[Theme]) -> list[str]:
    """A theme's schemes whose text is hard to read (``--check-theme`` warns)."""
    warnings = []
    for scheme in getattr(theme, "color_schemes", ()):
        if not isinstance(scheme, ColorScheme):
            continue
        try:
            colors = check_colors(scheme_colors(scheme))
        except ColorSchemeError:
            continue
        warnings += [f"color scheme {scheme.id!r}: {p}" for p in hard_to_read(colors)]
    return warnings
