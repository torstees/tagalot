"""Helpers for theme authors (docs/THEMES.md; #130): ``tagalot --new-theme ID`` starts a
theme from the template in the user themes folder, and ``tagalot --check-theme FILE`` loads
one theme file and lists what's wrong with it, as the launcher would."""

import keyword
import re
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from tagalot.themes.loader import default_user_themes_dir, load_theme_file, load_themes

Out = Callable[[str], None]

TEMPLATE_ID = 'id = "template"'
TEMPLATE_NAME = 'name = "Template"'
TEMPLATE_CLASS = "class TemplateTheme(Theme):"


class ThemeToolError(ValueError):
    """Something an author asked for can't be done; the message says why."""


def template_source() -> str:
    """The template theme's source (shipped with the app, packaged builds too)."""
    return (resources.files("tagalot.themes") / "template.py").read_text(encoding="utf-8")


def new_theme(theme_id: str, name: str | None = None, folder: Path | None = None) -> Path:
    """Write the template as ``<folder>/<theme_id>.py`` (the user themes folder by default),
    with its id, name, and class name filled in. Returns the file's path."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", theme_id) or keyword.iskeyword(theme_id):
        raise ThemeToolError(
            f"{theme_id!r} isn't a theme id: use lowercase letters, digits, and _ "
            "(starting with a letter), like my_photos"
        )
    taken = load_themes(user_dir=folder or default_user_themes_dir())
    if theme_id in taken.themes:
        raise ThemeToolError(
            f"there's already a theme {theme_id!r} ({taken.themes[theme_id].source})"
        )
    folder = folder or default_user_themes_dir()
    target = folder / f"{theme_id}.py"
    if target.exists():
        raise ThemeToolError(f"{target} already exists")
    title = name or theme_id.replace("_", " ").capitalize()
    class_name = "".join(part.capitalize() for part in theme_id.split("_")) + "Theme"
    source = template_source()
    for old, new in (
        (TEMPLATE_ID, f"id = {theme_id!r}"),
        (TEMPLATE_NAME, f"name = {title!r}"),
        (TEMPLATE_CLASS, f"class {class_name}(Theme):"),
    ):
        if old not in source:  # the template changed shape: a bug here, not the user's
            raise ThemeToolError(f"the template no longer has {old!r}")
        source = source.replace(old, new, 1)
    folder.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


def check_theme(path: Path, out: Out = print) -> int:
    """Load one theme file and report it: 0 when it loads cleanly, else 1."""
    catalog = load_theme_file(path)
    for loaded in catalog.themes.values():
        theme = loaded.theme
        out(f"ok: {theme.id!r} ({theme.name}, version {theme.version}) from {loaded.source}")
        out(f"   types: {', '.join(e.__name__ for e in theme.entities)}")
    for problem in catalog.problems:
        out(f"problem: {problem.message}")
    if not catalog.themes and not catalog.problems:
        out("problem: no theme found")
    return 0 if catalog.themes and not catalog.problems else 1


def run_new_theme(args: list[str], out: Out = print) -> int:
    """``--new-theme ID [--name NAME]``."""
    theme_id = _value(args, "--new-theme")
    if theme_id is None:
        out("Usage: tagalot --new-theme ID [--name NAME]")
        return 2
    try:
        path = new_theme(theme_id, _value(args, "--name"))
    except ThemeToolError as e:
        out(f"Can't make the theme: {e}")
        return 1
    out(f"Wrote {path}")
    out("Next: edit its TODO parts, check it with `tagalot --check-theme <file>`,")
    out("then create a keep with it (it's listed in the new-keep dialog).")
    return 0


def run_check_theme(args: list[str], out: Out = print) -> int:
    """``--check-theme FILE``."""
    path = _value(args, "--check-theme")
    if path is None:
        out("Usage: tagalot --check-theme FILE")
        return 2
    return check_theme(Path(path), out)


def _value(args: list[str], flag: str) -> str | None:
    if flag in args and args.index(flag) + 1 < len(args):
        value = args[args.index(flag) + 1]
        return None if value.startswith("--") else value
    return None
