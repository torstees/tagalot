"""Exports from the command line, for schedulers (#607): ``tagalot --export-tags`` writes the
same file as **Export tags…** (#609), which **Import tags…** loads back, and
``tagalot --export-keep`` the keep export (#612, ``core/keep_export.py``).

A keep is read as it is, never changed: one whose format isn't this build's is refused
(opening it in Tagalot upgrades it). A file inside a watched folder is refused unless the
caller allows it, since no one is there to confirm as the window asks; even then nothing
there is replaced (AGENTS.md rule 1). Files are written atomically, so a run that fails
leaves the last export whole."""

import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, Engine

from tagalot.core.db import create_keep_engine
from tagalot.core.keep import (
    KEEP_DB,
    KEEP_FORMAT_VERSION,
    Keep,
    KeepConfig,
    KeepError,
    open_keep,
)
from tagalot.core.keep_export import HEADER as KEEP_HEADER
from tagalot.core.keep_export import WIDTH as KEEP_WIDTH
from tagalot.core.keep_export import keep_data
from tagalot.core.roots import place_in_roots
from tagalot.core.settings import Settings
from tagalot.core.tag_yaml import (
    dump_yaml,
    export_tags,
    icon_files,
    picture_files,
    tagged_items,
)
from tagalot.core.tags import TagTree
from tagalot.core.theme_db import resolve_theme
from tagalot.core.theme_schema import ThemeSchema, build_theme_schema
from tagalot.core.tomlio import write_atomic
from tagalot.themes.loader import load_themes

logger = logging.getLogger(__name__)


class ExportError(Exception):
    """An export can't be made: the message says why. Nothing was written."""


@dataclass(frozen=True)
class ReadKeep:
    """A keep opened for reading only: its configuration, theme schema, and a reader."""

    keep: Keep
    schema: ThemeSchema
    reader: Engine


def read_keep(keep_dir: Path, settings: Settings) -> ReadKeep:
    """Open a keep to read from, changing nothing; the caller disposes of ``reader``.
    Raises :class:`ExportError` when it isn't a keep, needs upgrading or is newer, or its
    theme isn't available."""
    try:
        keep = open_keep(keep_dir)
    except KeepError as e:
        raise ExportError(str(e)) from e
    if not keep.db_path.is_file():
        raise ExportError(f"{keep.dir} has no {KEEP_DB}: it has never been opened")
    stored = keep.config.format_version
    if stored > KEEP_FORMAT_VERSION:
        raise ExportError(
            f"{keep.dir.name} was made by a newer Tagalot (keep format {stored}; this one "
            f"reads {KEEP_FORMAT_VERSION}): update Tagalot"
        )
    if stored < KEEP_FORMAT_VERSION:
        raise ExportError(
            f"{keep.dir.name} needs upgrading (keep format {stored}, now "
            f"{KEEP_FORMAT_VERSION}): open it in Tagalot once, then export"
        )
    try:
        theme = resolve_theme(load_themes(extra_dirs=settings.theme_dirs), keep)
    except KeepError as e:
        raise ExportError(str(e)) from e
    reader = create_keep_engine(keep.db_path, network=keep.on_network, read_only=True)
    return ReadKeep(keep, build_theme_schema(theme), reader)


@dataclass(frozen=True)
class TagExport:
    """What ``--export-tags`` wrote."""

    path: Path
    tags: int
    items: int | None  # items listed, with ``--items``
    pictures: int  # tag pictures and icons in the folder beside it
    watched: str | None  # the watched folder it is in, when allowed there


def export_tag_file(
    keep_dir: Path,
    target: Path,
    settings: Settings,
    *,
    items: bool = False,
    allow_watched: bool = False,
) -> TagExport:
    """Write the keep's tags as YAML to ``target`` (with which items have each tag, when
    ``items``), and their pictures in ``<name> pictures`` beside it, as the window's
    **Export tags…** does. Raises :class:`ExportError` before writing anything when it
    can't; an :class:`OSError` while writing leaves any earlier export whole."""

    def build(found: _Found) -> tuple[str, int | None]:
        listed = tagged_items(found.conn, found.schema, found.tree) if items else None
        text = export_tags(found.tree, found.schema, listed, found.pictures, found.icons)
        return text, len(listed) if listed is not None else None

    written = _export(keep_dir, target, settings, allow_watched, build)
    return TagExport(written.path, written.tags, written.count, written.pictures, written.watched)


@dataclass(frozen=True)
class KeepExport:
    """What ``--export-keep`` (and **Export keep…**) wrote (#612)."""

    path: Path
    tags: int
    items: int
    """Items with something set by hand."""
    pictures: int
    watched: str | None


def export_keep_file(
    keep_dir: Path, target: Path, settings: Settings, *, allow_watched: bool = False
) -> KeepExport:
    """Write the keep export (``core/keep_export.py``) to ``target``, with the tags'
    pictures beside it, as :func:`export_tag_file` writes the tags."""

    def build(found: _Found) -> tuple[str, int | None]:
        data = keep_data(
            found.conn, found.schema, found.tree, found.config, found.pictures, found.icons
        )
        return KEEP_HEADER + dump_yaml(data, KEEP_WIDTH), len(data.get("items", []))

    written = _export(keep_dir, target, settings, allow_watched, build)
    return KeepExport(
        written.path, written.tags, written.count or 0, written.pictures, written.watched
    )


@dataclass(frozen=True)
class _Found:
    """What an export is made from: an open reader's connection, and the tags' pictures'
    file names beside the file."""

    conn: Connection
    schema: ThemeSchema
    tree: TagTree
    config: KeepConfig
    pictures: Mapping[int, str]
    icons: Mapping[int, str]


@dataclass(frozen=True)
class _Written:
    path: Path
    tags: int
    count: int | None
    pictures: int
    watched: str | None


def _export(
    keep_dir: Path,
    target: Path,
    settings: Settings,
    allow_watched: bool,
    build: Callable[[_Found], tuple[str, int | None]],
) -> _Written:
    """Make an export with ``build`` (its text, and how many items it lists) from the keep
    opened read-only, and write it with the tags' pictures beside it. A file in a watched
    folder needs ``allow_watched``, and never replaces one there."""
    target = target.resolve()
    if target.is_dir():
        raise ExportError(f"{target} is a folder: give the file's name")
    opened = read_keep(keep_dir, settings)
    config = opened.keep.config
    folder = f"{target.stem} pictures"
    try:
        roots = {r.id: settings.root_path(config.id, r) for r in config.roots}
        place = place_in_roots(str(target), roots)
        with opened.reader.connect() as conn:
            tree = TagTree.load(conn)
            names = picture_files(tree, folder)
            icons = icon_files(tree, folder, names.values())
            text, count = build(_Found(conn, opened.schema, tree, config, names, icons))
    finally:
        opened.reader.dispose()
    files = {names[t]: p.data for t, p in tree.pictures().items() if t in names}
    files |= {icons[t]: p.data for t, p in tree.icon_pictures().items() if t in icons}
    watched = None
    if place is not None:
        root = next(r for r in config.roots if r.id == place[0])
        if not allow_watched:
            raise ExportError(
                f"{target} is in {root.name}, a folder this keep watches; add "
                "--allow-watched to save it there anyway (nothing there is replaced)"
            )
        taken = [p for p in (target, target.parent / folder) if p.exists()]
        if not files:
            taken = [p for p in taken if p == target]
        if taken:
            raise ExportError(
                f"{taken[0].name} is already in {root.name}, a folder this keep watches: "
                "Tagalot never replaces files there, so choose a new name"
            )
        watched = root.name
    target.parent.mkdir(parents=True, exist_ok=True)
    for relpath, data in files.items():
        _write_bytes(target.parent.joinpath(*relpath.split("/")), data)
    write_atomic(target, text)
    return _Written(target, len(tree), count, len(files), watched)


def _write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` through a temporary file beside ``path``, then replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
