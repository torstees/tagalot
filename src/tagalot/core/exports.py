"""Exports from the command line, for schedulers (#607): ``tagalot --export-tags`` writes the
same file as **Export tags…** (#609), which **Import tags…** loads back.

A keep is read as it is, never changed: one whose format isn't this build's is refused
(opening it in Tagalot upgrades it). A file inside a watched folder is refused unless the
caller allows it, since no one is there to confirm as the window asks; even then nothing
there is replaced (AGENTS.md rule 1). Files are written atomically, so a run that fails
leaves the last export whole."""

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from tagalot.core.db import create_keep_engine
from tagalot.core.keep import KEEP_DB, KEEP_FORMAT_VERSION, Keep, KeepError, open_keep
from tagalot.core.roots import place_in_roots
from tagalot.core.settings import Settings
from tagalot.core.tag_yaml import export_tags, icon_files, picture_files, tagged_items
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
    target = target.resolve()
    if target.is_dir():
        raise ExportError(f"{target} is a folder: give the file's name")
    opened = read_keep(keep_dir, settings)
    try:
        roots = {
            r.id: settings.root_path(opened.keep.config.id, r) for r in opened.keep.config.roots
        }
        place = place_in_roots(str(target), roots)
        folder = f"{target.stem} pictures"
        with opened.reader.connect() as conn:
            tree = TagTree.load(conn)
            listed = tagged_items(conn, opened.schema, tree) if items else None
    finally:
        opened.reader.dispose()
    names = picture_files(tree, folder)
    icons = icon_files(tree, folder, names.values())
    files = {names[t]: p.data for t, p in tree.pictures().items() if t in names}
    files |= {icons[t]: p.data for t, p in tree.icon_pictures().items() if t in icons}
    watched = None
    if place is not None:
        root = next(r for r in opened.keep.config.roots if r.id == place[0])
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
    text = export_tags(tree, opened.schema, listed, names, icons)
    target.parent.mkdir(parents=True, exist_ok=True)
    for relpath, data in files.items():
        _write_bytes(target.parent.joinpath(*relpath.split("/")), data)
    write_atomic(target, text)
    return TagExport(
        target, len(tree), len(listed) if listed is not None else None, len(files), watched
    )


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
