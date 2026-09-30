"""Theme discovery and declaration validation (DESIGN.md §9 "Discovery").

Themes come from, in order: the built-in themes, the user themes folder, and any extra
folders from settings. Each module must define exactly one :class:`Theme` subclass. Loading
never raises: every problem is recorded against its file so the keep launcher can show it,
and the remaining themes load normally. Validation needs no database.
"""

import hashlib
import importlib
import importlib.machinery
import importlib.util
import inspect
import logging
import pkgutil
import sys
import traceback
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import platformdirs

import tagalot.builtin_themes
from tagalot.core.theme_schema import SchemaBuildError, build_theme_schema
from tagalot.themes.api import (
    API_VERSION,
    DetailView,
    Entity,
    RoleImage,
    SearchView,
    Theme,
    ThemeDeclarationError,
    ThemeOption,
    ThumbnailProvider,
    entity_fields,
)

logger = logging.getLogger(__name__)

USER_MODULE_PREFIX = "tagalot_user_theme_"

MAX_THUMBNAIL_SIZE = 2048
"""The largest thumbnail a theme (or keep) may ask for, in pixels."""


def default_user_themes_dir() -> Path:
    """Where users drop theme files, e.g. ``%LOCALAPPDATA%\\tagalot\\themes`` on Windows."""
    return Path(platformdirs.user_data_dir("tagalot", appauthor=False)) / "themes"


@dataclass(frozen=True)
class ThemeProblem:
    """Why a theme (or a file that should hold one) can't be used."""

    source: str
    """The file, folder, or built-in module the problem is in."""
    message: str


@dataclass(frozen=True)
class LoadedTheme:
    theme: type[Theme]
    source: str
    builtin: bool


@dataclass
class ThemeCatalog:
    """Every theme that loaded, by id, and every problem found along the way."""

    themes: dict[str, LoadedTheme] = field(default_factory=dict)
    problems: list[ThemeProblem] = field(default_factory=list)

    def get(self, theme_id: str) -> LoadedTheme | None:
        return self.themes.get(theme_id)

    def problems_for(self, source: str) -> list[ThemeProblem]:
        return [p for p in self.problems if p.source == source]


def load_themes(
    extra_dirs: Iterable[Path] = (),
    user_dir: Path | None = None,
    *,
    include_builtin: bool = True,
) -> ThemeCatalog:
    """Discover, import, and validate every theme. Never raises."""
    catalog = ThemeCatalog()
    if include_builtin:
        for name, module_or_error in _builtin_modules():
            _add(catalog, name, module_or_error, builtin=True)
    folders = [user_dir if user_dir is not None else default_user_themes_dir(), *extra_dirs]
    for folder in folders:
        for source, module_or_error in _folder_modules(folder):
            _add(catalog, source, module_or_error, builtin=False)
    for problem in catalog.problems:
        logger.warning("Theme problem in %s: %s", problem.source, problem.message)
    return catalog


def validate_theme(theme: type[Theme]) -> list[str]:
    """Everything wrong with a theme's declarations, checked without a database."""
    problems: list[str] = []
    if not isinstance(getattr(theme, "name", None), str) or not theme.name.strip():
        problems.append("the theme needs a name")
    version = getattr(theme, "version", None)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        problems.append(f"version must be a positive integer, not {version!r}")
    api_version = getattr(theme, "api_version", API_VERSION)
    if not isinstance(api_version, int) or api_version > API_VERSION:
        problems.append(
            f"it needs theme API version {api_version}; this Tagalot provides {API_VERSION}"
        )
    for ext in getattr(theme, "extensions", ()):
        if not isinstance(ext, str) or not ext.startswith(".") or ext != ext.lower():
            problems.append(f"extension {ext!r} must be lowercase and start with '.'")

    entities = list(getattr(theme, "entities", ()))
    if not entities:
        problems.append("the theme declares no entities")
    if any(not (inspect.isclass(e) and issubclass(e, Entity)) for e in entities):
        return [*problems, "entities must be Entity subclasses"]
    declared = set(entities)
    names = {e: e.__name__ for e in entities}

    roles_of: dict[type[Entity], set[str]] = {}
    for entity in entities:
        role_names = [r.name for r in entity.roles]
        duplicates = sorted({n for n in role_names if role_names.count(n) > 1})
        if duplicates:
            problems.append(f"{names[entity]}: role {', '.join(duplicates)} is declared twice")
        primaries = [r.name for r in entity.roles if r.primary]
        if len(primaries) > 1:
            problems.append(
                f"{names[entity]}: only one role can be primary ({', '.join(primaries)})"
            )
        roles_of[entity] = set(role_names)

    for c in getattr(theme, "containment", ()):
        for side in (c.parent, c.child):
            if side not in declared:
                problems.append(
                    f"containment uses {side.__name__}, which the theme doesn't declare"
                )

    relationships = {r.name: r for r in getattr(theme, "relationships", ())}
    for view in getattr(theme, "views", ()):
        if isinstance(view, SearchView):
            unknown = [t.__name__ for t in view.types if t not in declared]
            if unknown:
                problems.append(f"search view {view.name!r} uses {', '.join(unknown)}")
            field_names = {"title", "created_at", "updated_at"}
            for t in view.types:
                if t in declared:
                    field_names |= _safe_field_names(t)
            for key in view.default_sort:
                if key.field not in field_names:
                    problems.append(f"search view {view.name!r} sorts by unknown {key.field!r}")
        elif isinstance(view, DetailView):
            if view.type not in declared:
                problems.append(f"detail view for {view.type.__name__}, which isn't declared")
                continue
            for section in view.sections:
                where = f"{names[view.type]} detail view"
                if section.kind in ("role", "gallery") and section.name not in roles_of[view.type]:
                    problems.append(f"{where}: no role named {section.name!r}")
                elif section.kind == "gallery" and not any(
                    r.name == section.name and r.many for r in view.type.roles
                ):
                    problems.append(
                        f"{where}: gallery {section.name!r} needs a role with many=True"
                    )
                elif section.kind == "related":
                    rel = relationships.get(section.name or "")
                    if rel is None:
                        problems.append(f"{where}: no relationship named {section.name!r}")
                    elif view.type not in (rel.a, rel.b):
                        problems.append(
                            f"{where}: relationship {section.name!r} doesn't involve "
                            f"{names[view.type]}"
                        )
        else:
            problems.append(f"views must be SearchView or DetailView, not {view!r}")

    all_roles = set().union(*roles_of.values()) if roles_of else set()
    for method, spec in theme.actions().items():
        for target in spec.applies_to:
            if isinstance(target, str) and target not in all_roles:
                problems.append(f"action {method!r} applies to unknown role {target!r}")
            elif inspect.isclass(target) and target not in declared:
                problems.append(f"action {method!r} applies to undeclared {target.__name__}")

    problems.extend(_chain_problems(theme, entities, roles_of))
    option_names = [getattr(o, "name", None) for o in theme.options]
    if not all(isinstance(o, ThemeOption) for o in theme.options):
        problems.append("options must be declared with option()")
    elif len(set(option_names)) != len(option_names):
        problems.append("an option is declared twice")
    for entity in entities:
        try:
            infos = entity_fields(entity)
        except ThemeDeclarationError:
            infos = []  # reported by the table build below
        for info in infos:
            if info.spec.display is not None and info.type not in (int, float):
                problems.append(
                    f"{names[entity]}.{info.name}: display={info.spec.display!r} needs a "
                    "number field (int or float)"
                )
        known = _safe_field_names(entity)
        for name in entity.card_lines:
            if name not in known:
                problems.append(f"{names[entity]}: card line {name!r} isn't one of its fields")
        sortable = {"title", "created_at", "updated_at"}
        for inside in _contained(theme, entity):
            sortable |= _safe_field_names(inside)
        for key in entity.contents_sort:
            if key.field not in sortable:
                problems.append(
                    f"{names[entity]}: contents_sort uses {key.field!r}, which nothing it "
                    "contains has"
                )
    maximum, default = theme.thumbnail_max, theme.thumbnail_default
    if not (isinstance(maximum, int) and 16 <= maximum <= MAX_THUMBNAIL_SIZE):
        problems.append(
            f"thumbnail_max must be a whole number from 16 to {MAX_THUMBNAIL_SIZE}, not {maximum!r}"
        )
    elif not (isinstance(default, int) and 16 <= default <= maximum):
        problems.append(
            f"thumbnail_default must be a whole number from 16 to thumbnail_max ({maximum}), "
            f"not {default!r}"
        )

    try:
        build_theme_schema(theme)
    except SchemaBuildError as e:
        problems.extend(e.problems)
    except Exception as e:  # a declaration so broken the builder itself fails
        problems.append(f"its tables can't be built: {e}")
    return problems


def _chain_problems(
    theme: type[Theme], entities: Sequence[type[Entity]], roles_of: dict[type[Entity], set[str]]
) -> list[str]:
    """Each entity type's thumbnail chain holds providers, and its roles exist."""
    problems = []
    try:
        instance = theme()
    except Exception as e:
        return [f"the theme can't be created: {e}"]
    for entity in entities:
        where = f"{entity.__name__} thumbnails"
        try:
            chain: list[object] = list(instance.thumbnail_chain(entity))
        except Exception as e:
            problems.append(f"{where}: thumbnail_chain failed: {e}")
            continue
        for provider in chain:
            if not isinstance(provider, ThumbnailProvider):
                problems.append(f"{where}: {provider!r} isn't a ThumbnailProvider")
            elif isinstance(provider, RoleImage) and provider.role not in roles_of[entity]:
                problems.append(f"{where}: no role named {provider.role!r}")
    return problems


def _contained(theme: type[Theme], entity: type[Entity]) -> set[type[Entity]]:
    """The types ``entity`` can contain, at any depth."""
    edges = [(c.parent, c.child) for c in getattr(theme, "containment", ())]
    found: set[type[Entity]] = set()
    frontier = [entity]
    while frontier:
        current = frontier.pop()
        for parent, child in edges:
            if parent is current and child not in found:
                found.add(child)
                frontier.append(child)
    return found


def _safe_field_names(entity: type[Entity]) -> set[str]:
    try:
        return {f.name for f in entity_fields(entity)}
    except ThemeDeclarationError:
        return set()  # reported by the schema build


def _add(
    catalog: ThemeCatalog, source: str, module_or_error: ModuleType | str, *, builtin: bool
) -> None:
    if isinstance(module_or_error, str):
        catalog.problems.append(ThemeProblem(source, module_or_error))
        return
    module = module_or_error
    themes = [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj)
        and issubclass(obj, Theme)
        and obj is not Theme
        and obj.__module__ == module.__name__
    ]
    if not themes:
        if not builtin:  # built-in modules without a theme are placeholders for later themes
            catalog.problems.append(ThemeProblem(source, "it defines no Theme subclass"))
        return
    if len(themes) > 1:
        names = ", ".join(sorted(t.__name__ for t in themes))
        catalog.problems.append(
            ThemeProblem(source, f"it defines {len(themes)} Theme subclasses ({names}); use one")
        )
        return
    theme = themes[0]
    theme_id = getattr(theme, "id", None)
    if not isinstance(theme_id, str) or not theme_id.isidentifier() or theme_id != theme_id.lower():
        catalog.problems.append(
            ThemeProblem(source, f"theme id {theme_id!r} must be a lowercase identifier")
        )
        return
    if theme_id in catalog.themes:
        other = catalog.themes[theme_id].source
        catalog.problems.append(
            ThemeProblem(source, f"theme id {theme_id!r} is already provided by {other}")
        )
        return
    try:
        problems = validate_theme(theme)
    except Exception as e:
        problems = [f"validation failed: {e}"]
    if problems:
        catalog.problems.extend(ThemeProblem(source, p) for p in problems)
        return
    catalog.themes[theme_id] = LoadedTheme(theme, source, builtin)


def _builtin_modules() -> Iterator[tuple[str, ModuleType | str]]:
    package = tagalot.builtin_themes
    for info in pkgutil.iter_modules(package.__path__, f"{package.__name__}."):
        try:
            yield info.name, importlib.import_module(info.name)
        except Exception:
            yield info.name, _describe_import_error()


def _folder_modules(folder: Path) -> Iterator[tuple[str, ModuleType | str]]:
    if not folder.is_dir():
        return
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if path.name.startswith(("_", ".")):
            continue
        if path.is_file() and path.suffix == ".py":
            yield str(path), _import_file(path, path)
        elif path.is_dir() and (path / "__init__.py").is_file():
            yield str(path), _import_file(path / "__init__.py", path)


def _import_file(file: Path, source: Path) -> ModuleType | str:
    """Import a theme file under a unique module name (reloading replaces the old one)."""
    digest = hashlib.sha1(str(source.resolve()).encode()).hexdigest()[:12]
    name = f"{USER_MODULE_PREFIX}{digest}"
    for existing in [m for m in sys.modules if m == name or m.startswith(f"{name}.")]:
        del sys.modules[existing]
    locations = [str(source)] if source.is_dir() else None
    spec = importlib.util.spec_from_file_location(
        name, file, loader=_FreshSourceLoader(name, str(file)), submodule_search_locations=locations
    )
    if spec is None or spec.loader is None:
        return "it can't be imported"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses and typing look modules up here
    try:
        spec.loader.exec_module(module)
    except (Exception, SystemExit):  # a theme calling sys.exit() must not quit the app
        del sys.modules[name]
        return _describe_import_error()
    return module


class _FreshSourceLoader(importlib.machinery.SourceFileLoader):
    """Always compiles a theme from source and never reads or writes ``.pyc`` files.

    Python validates cached bytecode by modification time (to the second) and size, so a
    quick edit that keeps the file's size (``version = 1`` to ``version = 2``) could reload
    the old code. Theme files are small; compiling them is cheap.
    """

    def get_code(self, fullname: str) -> Any:
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


def _describe_import_error() -> str:
    """The last line of the traceback, plus where in the theme file it happened."""
    exc_type, exc, tb = sys.exc_info()
    frames = [f for f in traceback.extract_tb(tb) if "importlib" not in f.filename]
    where = f" (line {frames[-1].lineno})" if frames else ""
    if isinstance(exc, SyntaxError):
        where = f" (line {exc.lineno})"
    name = exc_type.__name__ if exc_type else "Error"
    return f"it failed to load{where}: {name}: {exc}"
