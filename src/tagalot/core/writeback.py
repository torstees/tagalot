"""Writing metadata back into files: **Write to file…** (DESIGN.md §4 "Writing back to
files", #299).

This is the one code path that changes a file under a root (AGENTS.md rule 1), and it is
narrow on purpose:

- only files in roots marked ``writable = true``, only Markdown files of the theme's
  :attr:`~tagalot.themes.api.Theme.write_back` types, and only when the user asks;
- :func:`plan_write_back` (a worker) reads each file and works out its new front matter,
  for the user to preview; a file that changed since Tagalot last read it is refused;
- :func:`write_files` checks each file again, copies it to the keep's ``backups/`` folder,
  and replaces it, changing only the front-matter block: the rest of the file is kept byte
  for byte.

YAML front matter is edited round-trip with ruamel.yaml, so comments, quoting, and the
order of keys stay; TOML front matter has the lines of the keys being written replaced,
leaving every other line as it was. Tags are written under ``tags``: the file's own words
for tags its keywords gave (a word whose tag the user took off the item goes), then the
item's other tags as full paths (``Genre/Fantasy``).
"""

import io
import logging
import os
import re
import shutil
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import tomli_w
from sqlalchemy import Connection, select

from tagalot.core.keywords import keyword_index, keyword_key
from tagalot.core.models import (
    Entity,
    EntityResource,
    EntityTag,
    FieldProvenance,
    FieldSource,
    FileTagRemoval,
    Resource,
    ResourceStatus,
)
from tagalot.core.roots import local_path
from tagalot.core.tags import TagTree
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import EntityRef, Record, split_keywords

logger = logging.getLogger(__name__)

MARKDOWN = frozenset({".md", ".markdown"})
"""The files Write to file… can change: Markdown, in their front matter."""
TAG_KEYS = ("tags", "keywords")
"""Front-matter keys holding tags; new tags go under ``tags``."""
BACKUPS = "backups"
"""The keep's folder of copies of files before Tagalot changed them."""


class WriteBackError(ValueError):
    """A file's front matter can't be read or written."""


# --- the front-matter block ---


@dataclass(frozen=True)
class FrontMatter:
    """Where a Markdown text's front matter is: ``text[start:end]`` is the block between
    its marker lines, ``style`` ``"yaml"`` (``---``) or ``"toml"`` (``+++``), or ``None``
    when the text has none (``start`` is then where one would go)."""

    style: str | None
    start: int
    end: int


def find_front_matter(text: str) -> FrontMatter:
    """Find the front-matter block of a Markdown text (after a byte-order mark, if any)."""
    offset = 1 if text.startswith("﻿") else 0
    lines = text[offset:].splitlines(keepends=True)
    marker = lines[0].strip() if lines else ""
    if marker not in ("---", "+++"):
        return FrontMatter(None, offset, offset)
    ends = ("---", "...") if marker == "---" else ("+++",)
    position = offset + len(lines[0])
    for line in lines[1:]:
        if line.strip() in ends:
            return FrontMatter(
                "yaml" if marker == "---" else "toml", offset + len(lines[0]), position
            )
        position += len(line)
    return FrontMatter(None, offset, offset)  # an opening line only: a thematic break


def _yaml() -> Any:
    from ruamel.yaml import YAML

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096  # never fold a long value onto two lines
    return yaml


def read_block(style: str | None, block: str) -> dict[str, Any]:
    """The keys of a front-matter block, as written (an empty block has none). Raises
    :class:`WriteBackError` for a block that doesn't parse."""
    if style is None or not block.strip():
        return {}
    if style == "toml":
        try:
            return tomllib.loads(block)
        except tomllib.TOMLDecodeError as e:
            raise WriteBackError(f"its TOML front matter doesn't parse: {e}") from e
    from ruamel.yaml.error import YAMLError

    try:
        data = _yaml().load(block)
    except YAMLError as e:
        raise WriteBackError(f"its YAML front matter doesn't parse: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise WriteBackError("its YAML front matter isn't a set of keys and values")
    return data


def key_in(data: Mapping[str, Any], key: str) -> str | None:
    """The key of ``data`` matching ``key``: exactly, else ignoring case."""
    if key in data:
        return key
    return next((k for k in data if str(k).casefold() == key.casefold()), None)


def edit_front_matter(text: str, changes: Mapping[str, Any]) -> str:
    """``text`` with its front matter's keys set as in ``changes`` (``None`` removes one);
    everything outside the block stays exactly as it was. A text without front matter
    gains a YAML block at the top. Raises :class:`WriteBackError` for a block that doesn't
    parse."""
    if not changes:
        return text
    found = find_front_matter(text)
    if found.style is None:
        values = {k: v for k, v in changes.items() if v is not None}
        if not values:
            return text
        newline = "\r\n" if "\r\n" in text else "\n"
        block = _edit_yaml("", values).replace("\n", newline)
        head = text[: found.start]
        return f"{head}---{newline}{block}---{newline}{text[found.start :]}"
    block = text[found.start : found.end]
    crlf = "\r\n" in block
    plain = block.replace("\r\n", "\n")
    edited = _edit_yaml(plain, changes) if found.style == "yaml" else _edit_toml(plain, changes)
    if crlf:
        edited = edited.replace("\n", "\r\n")
    return text[: found.start] + edited + text[found.end :]


def _edit_yaml(block: str, changes: Mapping[str, Any]) -> str:
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.util import load_yaml_guess_indent

    yaml = _yaml()
    data: Any = read_block("yaml", block) if block.strip() else CommentedMap()
    if not isinstance(data, CommentedMap):
        data = CommentedMap(data)
    if block.strip():
        _, indent, offset = load_yaml_guess_indent(block)
        if indent is not None:  # write new lists as the file writes its own
            yaml.indent(mapping=indent, sequence=indent, offset=offset or 0)
    for key, value in changes.items():
        name = key_in(data, key)
        if value is None:
            if name is not None:
                del data[name]
            continue
        old = data.get(name) if name is not None else None
        if isinstance(old, CommentedSeq) and isinstance(value, list):
            old[:] = value  # in place: the list keeps its style ([a, b] or one per line)
        else:
            data[name if name is not None else key] = value
    out = io.StringIO()
    yaml.dump(data, out)
    dumped = out.getvalue()
    return "" if dumped.strip() in ("{}", "") else dumped


_TOML_KEY = re.compile(r"""^\s*("(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_-]+)\s*=""")
_TOML_TABLE = re.compile(r"^\s*\[")


def _toml_line(key: str, value: Any) -> str:
    """``key = value`` as one TOML line (a list inline: ``tags = ["a", "b"]``)."""
    head = tomli_w.dumps({key: 0}).partition(" = ")[0]
    if isinstance(value, list):
        items = [tomli_w.dumps({"x": v}).partition(" = ")[2].strip() for v in value]
        return f"{head} = [{', '.join(items)}]"
    return tomli_w.dumps({key: value}).strip()


def _edit_toml(block: str, changes: Mapping[str, Any]) -> str:
    """Replace the lines of the top-level keys in ``changes``; add new ones after the last
    top-level key. Other lines, comments included, stay as they are."""
    lines = block.split("\n")
    tables = next((i for i, line in enumerate(lines) if _TOML_TABLE.match(line)), len(lines))
    starts = [
        (i, m.group(1).strip("\"'")) for i in range(tables) if (m := _TOML_KEY.match(lines[i]))
    ]
    spans: dict[str, tuple[int, int]] = {}
    for n, (start, name) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else tables
        while end > start + 1 and (
            not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
        ):
            end -= 1  # blank lines and comments after a value aren't its own
        spans.setdefault(name, (start, end))
    edits: list[tuple[int, int, list[str]]] = []
    added: list[str] = []
    for key, value in changes.items():
        name = key_in(spans, key)
        new = [] if value is None else [_toml_line(key if name is None else name, value)]
        if name is not None:
            edits.append((*spans[name], new))
        elif value is not None:
            added += new
    if added:  # after the last top-level key: every span ends at or before it, so stays put
        last = max((end for _, end in spans.values()), default=0)
        lines[last:last] = added
    for start, end, new in sorted(edits, reverse=True):
        lines[start:end] = new
    return "\n".join(lines)


# --- what to write ---


def tag_changes(
    current: Mapping[str, Any], item_tags: Iterable[int], removed: Iterable[int], tree: TagTree
) -> dict[str, list[str]]:
    """The tag lists to write: of each of the file's tag keys (``tags``, ``keywords``), its
    own words, less those whose tag the user took off the item (``removed``); then, under
    ``tags``, the item's other tags as full paths (``Genre/Fantasy``). Only lists that
    change are given."""
    index = keyword_index(tree)
    off = set(removed)
    covered: set[int] = set()
    present: dict[str, tuple[list[str], list[str]]] = {}
    for key in TAG_KEYS:
        name = key_in(current, key)
        if name is None:
            continue
        words = split_keywords(current[name])
        kept = [w for w in words if index.get(keyword_key(w)) not in off]
        covered |= {t for w in kept if (t := index.get(keyword_key(w))) is not None}
        present[name] = (words, kept)
    missing = [t for t in set(item_tags) if t in tree and t not in covered]
    added = sorted(("/".join(tree.path(t)) for t in missing), key=str.casefold)
    target = key_in(current, "tags") or key_in(current, "keywords") or "tags"
    changes: dict[str, list[str]] = {}
    for name, (words, kept) in present.items():
        new = kept + (added if name == target else [])
        if new != words:
            changes[name] = new
    if target not in present and added:
        changes[target] = added
    return changes


def field_changes(
    theme: Any,
    entity_type: type,
    record: Record,
    edited: frozenset[str],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """The theme's keys for the fields the user edited, less those already as written."""
    wanted = theme.front_matter(entity_type, record, edited, current) if edited else {}
    changes: dict[str, Any] = {}
    for key, value in wanted.items():
        name = key_in(current, key)
        if value is None and name is None:
            continue
        if name is not None and current[name] == value:
            continue
        changes[name if name is not None else key] = value
    return changes


# --- planning (a worker) ---


@dataclass(frozen=True)
class FileWrite:
    """What Write to file… would do to one file: its front matter before and after, or
    why it can't."""

    entity_id: int
    title: str
    resource_id: int
    root_id: str
    relpath: str
    path: str
    size: int | None
    mtime_ns: int | None
    before: str = ""
    """The front-matter block now (empty when the file has none)."""
    after: str = ""
    """The front-matter block it would have."""
    text: bytes | None = None
    """The file's new content, when it changes."""
    problem: str | None = None
    """Why the file can't be written (it is left alone)."""

    @property
    def changes(self) -> bool:
        return self.problem is None and self.text is not None


@dataclass
class WritePlan:
    """The files Write to file… would change, and the items it can't write, with why."""

    files: list[FileWrite] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(item title, reason)`` for items with no file to write."""


def plan_write_back(
    conn: Connection,
    schema: ThemeSchema,
    tree: TagTree,
    entity_ids: Sequence[int],
    root_paths: Mapping[str, str | None],
    writable: Iterable[str],
) -> WritePlan:
    """Work out, for each item, the new front matter of its Markdown files (its primary
    role's), reading them (call it from a worker). ``root_paths`` gives this computer's path
    of each root (``None``: none here); only roots in ``writable`` are written."""
    from tagalot.core.ingest import IngestSession

    theme = schema.theme()
    types = {schema.theme.type_id_of(t): t for t in schema.theme.write_back}
    allowed = set(writable)
    plan = WritePlan()
    context = IngestSession(conn, schema)  # only to read records; nothing is written
    rows = conn.execute(
        select(Entity.id, Entity.title, Entity.type).where(Entity.id.in_(entity_ids))
    )
    for entity_id, title, type_id in sorted(rows, key=lambda r: list(entity_ids).index(r[0])):
        entity_type = types.get(type_id)
        if entity_type is None:
            plan.skipped.append((title, "its type has nothing Tagalot writes back"))
            continue
        files = _markdown_files(conn, schema, entity_id, entity_type)
        if not files:
            plan.skipped.append((title, "it has no Markdown file"))
            continue
        record = context.get(EntityRef(entity_id, type_id))
        edited = frozenset(
            conn.scalars(
                select(FieldProvenance.field).where(
                    FieldProvenance.entity_id == entity_id,
                    FieldProvenance.source == FieldSource.USER,
                )
            )
        )
        item_tags = set(
            conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id == entity_id))
        )
        removed = set(
            conn.scalars(select(FileTagRemoval.tag_id).where(FileTagRemoval.entity_id == entity_id))
        )
        for resource_id, root_id, relpath, size, mtime in files:
            base = root_paths.get(root_id)
            path = local_path(base, relpath) if base else ""
            write = FileWrite(entity_id, title, resource_id, root_id, relpath, path, size, mtime)
            if root_id not in allowed:
                plan.files.append(
                    _problem(
                        write, "its folder doesn't allow writing (Keep configuration → Folders)"
                    )
                )
            elif not base:
                plan.files.append(_problem(write, "its folder has no path on this computer"))
            else:
                plan.files.append(
                    _plan_file(write, theme, entity_type, record, edited, item_tags, removed, tree)
                )
    return plan


def _problem(write: FileWrite, problem: str) -> FileWrite:
    return FileWrite(**{**write.__dict__, "problem": problem})


def _markdown_files(
    conn: Connection, schema: ThemeSchema, entity_id: int, entity_type: type
) -> list[tuple[int, str, str, int | None, int | None]]:
    primary = next((r.name for r in entity_type.roles if r.primary), None)  # type: ignore[attr-defined]
    query = (
        select(
            Resource.id,
            Resource.root_id,
            Resource.relpath,
            Resource.size,
            Resource.mtime_ns,
            Resource.ext,
        )
        .join(EntityResource, EntityResource.resource_id == Resource.id)
        .where(EntityResource.entity_id == entity_id, Resource.status == ResourceStatus.OK)
        .order_by(Resource.relpath)
    )
    if primary is not None:
        query = query.where(EntityResource.role == primary)
    return [
        (rid, root, rel, size, mtime)
        for rid, root, rel, size, mtime, ext in conn.execute(query)
        if ext in MARKDOWN
    ]


def _plan_file(
    write: FileWrite,
    theme: Any,
    entity_type: type,
    record: Record,
    edited: frozenset[str],
    item_tags: set[int],
    removed: set[int],
    tree: TagTree,
) -> FileWrite:
    try:
        stat = os.stat(write.path)
        if (stat.st_size, stat.st_mtime_ns) != (write.size, write.mtime_ns):
            return _problem(write, "it changed since Tagalot last read it: scan it, then try again")
        with open(write.path, "rb") as file:
            raw = file.read()
    except OSError as e:
        return _problem(write, f"it can't be read: {e.strerror or e}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _problem(write, "it isn't UTF-8 text")
    found = find_front_matter(text)
    before = text[found.start : found.end].replace("\r\n", "\n")
    try:
        current = read_block(found.style, before)
        changes: dict[str, Any] = {**field_changes(theme, entity_type, record, edited, current)}
        changes.update(tag_changes(current, item_tags, removed, tree))
        new = edit_front_matter(text, changes)
    except (WriteBackError, ValueError, TypeError) as e:
        return _problem(write, str(e))
    if new == text:
        return FileWrite(**{**write.__dict__, "before": before, "after": before})
    after_found = find_front_matter(new)
    after = new[after_found.start : after_found.end].replace("\r\n", "\n")
    return FileWrite(
        **{**write.__dict__, "before": before, "after": after, "text": new.encode("utf-8")}
    )


# --- writing ---


@dataclass(frozen=True)
class WriteResult:
    write: FileWrite
    error: str | None = None
    backup: Path | None = None


def write_files(
    writes: Iterable[FileWrite],
    keep_dir: Path,
    writable: Iterable[str],
    now: datetime | None = None,
) -> list[WriteResult]:
    """Write the planned files that change (call it from a worker): each is checked again
    (its folder still writable, the file as it was when planned), copied to
    ``<keep>/backups/<time>/<root id>/<path>``, then replaced (a new file beside it, renamed
    over it). A file that fails is reported and left as it was."""
    allowed = set(writable)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    results: list[WriteResult] = []
    for write in writes:
        if not write.changes or write.text is None:
            continue
        if write.root_id not in allowed:
            results.append(WriteResult(write, "its folder no longer allows writing"))
            continue
        target = Path(write.path)
        try:
            stat = target.stat()
            if (stat.st_size, stat.st_mtime_ns) != (write.size, write.mtime_ns):
                results.append(
                    WriteResult(write, "it changed after the preview; nothing was written")
                )
                continue
            backup = keep_dir / BACKUPS / stamp / write.root_id / Path(*write.relpath.split("/"))
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            temp = target.with_name(f".{target.name}.tagalot-{os.getpid()}.tmp")
            try:
                temp.write_bytes(write.text)
                shutil.copymode(target, temp)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Couldn't write %s: %s", write.path, e)
            results.append(WriteResult(write, f"it couldn't be written: {e.strerror or e}"))
            continue
        logger.info("Wrote the front matter of %s (backup %s)", write.path, backup)
        results.append(WriteResult(write, None, backup))
    return results
