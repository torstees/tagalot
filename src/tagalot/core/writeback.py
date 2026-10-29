"""Writing metadata back into files: **Write to file…** (DESIGN.md §4 "Writing back to
files", #299).

This is the one code path that changes a file under a root (AGENTS.md rule 1), and it is
narrow on purpose:

- only files in roots marked ``writable = true`` (or, for notes in the keep's notes
  folder, a notes folder marked ``[notes] writable``), only Markdown files: an item's own
  (its primary role's, in any theme) and its note (#501), and only when the user asks;
- :func:`plan_write_back` (a worker) reads each file and works out its new front matter,
  for the user to preview; a file that changed since Tagalot last read it is refused;
- :func:`write_files` checks each file again, copies it to the keep's ``backups/`` folder,
  and replaces it, changing only the front-matter block: the rest of the file is kept byte
  for byte.

YAML front matter is edited round-trip with ruamel.yaml, so comments, quoting, and the
order of keys stay; TOML front matter has the lines of the keys being written replaced,
leaving every other line as it was. Tags are written under ``tags``: the file's own words
for tags its keywords gave (a word whose tag the user took off the item goes), then the
item's other tags as full paths (``Genre/Fantasy``). Fields the user edited go under the
theme's keys (:meth:`~tagalot.themes.api.Theme.front_matter`), else, and always in a note,
under the key the file already uses for the field (its name or label, as notes are read),
else its name (:func:`default_front_matter`).
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

from tagalot.core.extra_keys import aliased_fields, applied_aliases, field_keys, loose_key
from tagalot.core.keywords import keyword_index, keyword_key
from tagalot.core.models import (
    Entity,
    EntityNote,
    EntityResource,
    EntityTag,
    FieldProvenance,
    FieldSource,
    FileTagRemoval,
    NotesFolderNote,
    Resource,
    ResourceStatus,
)
from tagalot.core.own_fields import OwnFields
from tagalot.core.own_values import item_values
from tagalot.core.roots import local_path
from tagalot.core.tags import TagTree
from tagalot.core.theme_schema import ThemeSchema
from tagalot.themes.api import EntityRef, Record, Theme, entity_fields, split_keywords
from tagalot.themes.readers import front_matter_problems

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
    from ruamel.yaml.constructor import DuplicateKeyError
    from ruamel.yaml.error import YAMLError

    try:
        data = _yaml().load(block)
    except DuplicateKeyError as e:  # said plainly, as Triage → Markdown says it (#438)
        found = front_matter_problems(f"---\n{block}\n---\n")
        why = found[0] if found else "A key is given twice."
        raise WriteBackError(f"its front matter gives a key twice: {why} Remove one first") from e
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


def _loose(key: str) -> str:
    """A key as notes match them: ignoring case, spaces, ``_``, and ``-``."""
    return re.sub(r"[\s_-]", "", key.casefold())


def default_front_matter(
    entity_type: type, record: Record, edited: frozenset[str], current: Mapping[str, Any]
) -> dict[str, Any]:
    """The edited fields (and title) under the key the file already uses for each (its
    name or label, ignoring case, spaces, ``_``, and ``-``, as notes are read), else its name
    (#501). Used for notes, and for a theme that maps no keys of its own."""
    fields = {f.name: f for f in entity_fields(entity_type)}
    by_key = {_loose(str(k)): str(k) for k in current}
    wanted: dict[str, Any] = {}
    for name in sorted(edited):
        value: Any
        if name == "title":
            label, value = "title", record.title
        elif name in fields:
            label, value = str(fields[name].spec.label or name), record.fields.get(name)
        else:
            continue
        key = by_key.get(_loose(name)) or by_key.get(_loose(label)) or name
        wanted[key] = value
    return wanted


def aliased_keys(
    entity_type: type,
    wanted: Mapping[str, Any],
    edited: frozenset[str],
    current: Mapping[str, Any],
    aliases: Mapping[str, str],
) -> dict[str, Any]:
    """``wanted`` with each edited field the file holds under one of the user's aliased keys
    (#411), and not under its own name or label, moved to that key: the file keeps the key
    it uses, as it does for a theme's own alternatives (``synopsis``)."""
    infos = entity_fields(entity_type)
    own, aliased = field_keys(infos), aliased_fields(infos, aliases)
    held = {own[k].name for c in current if (k := loose_key(str(c))) in own}
    result = dict(wanted)
    for key in current:
        info = aliased.get(loose_key(str(key)))
        if info is None or info.name not in edited or info.name in held:
            continue
        for name in list(result):
            if own.get(loose_key(name)) is info:
                result[str(key)] = result.pop(name)
                break
    return result


def _maps_keys(theme: Theme) -> bool:
    """The theme says which keys its fields are written under."""
    return type(theme).front_matter is not Theme.front_matter


def field_changes(
    theme: Any,
    entity_type: type,
    record: Record,
    edited: frozenset[str],
    current: Mapping[str, Any],
    note: bool = False,
    aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The keys for the fields the user edited (the theme's, or, in a note or for a theme
    without its own, :func:`default_front_matter`), less those already as written. A field
    the file holds under one of the user's ``aliases`` (#411) is written under that key."""
    if not edited:
        wanted: Mapping[str, Any] = {}
    elif note or not _maps_keys(theme):
        wanted = default_front_matter(entity_type, record, edited, current)
    else:
        wanted = theme.front_matter(entity_type, record, edited, current)
    if aliases:
        wanted = aliased_keys(entity_type, wanted, edited, current, aliases)
    changes: dict[str, Any] = {}
    for key, value in wanted.items():
        name = key_in(current, key)
        if value is None and name is None:
            continue
        if name is not None and current[name] == value:
            continue
        changes[name if name is not None else key] = value
    return changes


def own_changes(
    current: Mapping[str, Any], values: Mapping[str, tuple[str, Any]]
) -> dict[str, Any]:
    """The keys for the keep's own fields the user edited (#584): ``values`` maps each
    field's name to ``(label, value)``. Each is written under the key the file already
    uses for it (its name or label, as notes read them), else its name; a value already
    as written is left out."""
    by_key = {_loose(str(k)): str(k) for k in current}
    changes: dict[str, Any] = {}
    for name, (label, value) in values.items():
        key = by_key.get(_loose(name)) or by_key.get(_loose(label)) or name
        if key in current and current[key] == value:
            continue
        if value is None and key not in current:
            continue
        changes[key] = value
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
    note: str | None = None
    """``"note"`` for the item's note beside its files, ``"notes folder"`` for its note in
    the keep's notes folder (#501); ``None`` for the item's own file."""

    @property
    def changes(self) -> bool:
        return self.problem is None and self.text is not None


@dataclass
class WritePlan:
    """The files Write to file… would change, and the items it can't write, with why."""

    files: list[FileWrite] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(item title, reason)`` for items with no file to write."""


INSIDE_ARCHIVE = "it's inside an archive, and Tagalot never changes archives"
"""Why a file inside an archive (#391) isn't written (#524)."""

NOTES_FOLDER_LOCKED = (
    "the notes folder doesn't let Write to file… change notes (Keep configuration → Notes, "
    "or its watched folder's Write back)"
)


def plan_write_back(
    conn: Connection,
    schema: ThemeSchema,
    tree: TagTree,
    entity_ids: Sequence[int],
    root_paths: Mapping[str, str | None],
    writable: Iterable[str],
    notes_write_back: bool = False,
    own_fields: OwnFields | None = None,
) -> WritePlan:
    """Work out, for each item, the new front matter of its Markdown files (its primary
    role's) and of its note (#501), reading them (call it from a worker). ``root_paths``
    gives this computer's path of each root (``None``: none here); only roots in
    ``writable`` are written, and notes in the notes folder also when ``notes_write_back``.
    The keep's ``own_fields`` (#584) the user edited are written under their names too."""
    from tagalot.core.ingest import IngestSession

    theme = schema.theme()
    aliases = applied_aliases(conn)
    types = {t.type_id: t.entity for t in schema.entities.values()}
    allowed = set(writable)
    plan = WritePlan()
    context = IngestSession(conn, schema)  # only to read records; nothing is written
    rows = conn.execute(
        select(Entity.id, Entity.title, Entity.type).where(Entity.id.in_(entity_ids))
    )
    for entity_id, title, type_id in sorted(rows, key=lambda r: list(entity_ids).index(r[0])):
        entity_type = types.get(type_id)
        if entity_type is None:
            plan.skipped.append((title, "its type isn't in this keep's theme"))
            continue
        found = _markdown_files(conn, schema, entity_id, entity_type)
        members = {f[0] for f in found if f[5]}
        files: list[tuple[int, str, str, int | None, int | None, str | None]] = [
            (*f[:5], None) for f in found
        ]
        files += _note_files(conn, entity_id, {f[0] for f in files})
        if not files:
            plan.skipped.append((title, "it has no Markdown file or note file"))
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
        mine = [f for f in (own_fields or {}).get(type_id, {}).values() if f.key in edited]
        given = item_values(conn, entity_id, mine)
        own = {f.name: (f.label, given.get(f.name)) for f in mine}
        item_tags = set(
            conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id == entity_id))
        )
        removed = set(
            conn.scalars(select(FileTagRemoval.tag_id).where(FileTagRemoval.entity_id == entity_id))
        )
        for resource_id, root_id, relpath, size, mtime, note in files:
            base = root_paths.get(root_id)
            path = local_path(base, relpath) if base else ""
            write = FileWrite(
                entity_id, title, resource_id, root_id, relpath, path, size, mtime, note=note
            )
            shared = _shared_note(conn, resource_id, entity_id) if note else None
            if resource_id in members:  # never written: said so, not left out (#524)
                plan.files.append(_problem(write, INSIDE_ARCHIVE))
            elif not may_write(write, allowed, notes_write_back):
                plan.files.append(
                    _problem(
                        write,
                        NOTES_FOLDER_LOCKED
                        if note == "notes folder"
                        else "its folder doesn't allow writing (Keep configuration → Folders)",
                    )
                )
            elif shared is not None:
                plan.files.append(
                    _problem(
                        write,
                        f"it's also the note of {shared!r}, which its tags would go to as well",
                    )
                )
            elif not base:
                plan.files.append(_problem(write, "its folder has no path on this computer"))
            else:
                plan.files.append(
                    _plan_file(
                        write,
                        theme,
                        entity_type,
                        record,
                        edited,
                        item_tags,
                        removed,
                        tree,
                        aliases,
                        own,
                    )
                )
    return plan


def may_write(write: FileWrite, writable: Iterable[str], notes_write_back: bool) -> bool:
    """Whether ``write``'s folder allows writing: its root is writable, or it is a note in
    the notes folder and the notes folder is (#501)."""
    return write.root_id in set(writable) or (write.note == "notes folder" and notes_write_back)


def _note_files(
    conn: Connection, entity_id: int, own: set[int]
) -> list[tuple[int, str, str, int | None, int | None, str | None]]:
    """The item's note files: beside its files, and in the notes folder (#501)."""
    columns = (Resource.id, Resource.root_id, Resource.relpath, Resource.size, Resource.mtime_ns)
    found: list[tuple[int, str, str, int | None, int | None, str | None]] = []
    # The notes folder's first: those notes are items' notes too.
    for kind, query in (
        (
            "notes folder",
            select(*columns)
            .join(NotesFolderNote, NotesFolderNote.resource_id == Resource.id)
            .where(NotesFolderNote.item_id == entity_id),
        ),
        (
            "note",
            select(*columns)
            .join(EntityNote, EntityNote.resource_id == Resource.id)
            .where(EntityNote.entity_id == entity_id),
        ),
    ):
        for rid, root, rel, size, mtime in conn.execute(
            # Never a file inside an archive (#391): nothing writes into one.
            query.where(Resource.status == ResourceStatus.OK, Resource.parent_resource_id.is_(None))
        ):
            if rid not in own and rel.lower().endswith(tuple(MARKDOWN)):
                own.add(rid)
                found.append((rid, root, rel, size, mtime, kind))
    return found


def _shared_note(conn: Connection, resource_id: int, entity_id: int) -> str | None:
    """The title of another item whose note this file also is (a movie's folder note is its
    Folder's too), if any: writing it would tag that item as well."""
    other = conn.scalar(
        select(Entity.title)
        .join(EntityNote, EntityNote.entity_id == Entity.id)
        .where(EntityNote.resource_id == resource_id, Entity.id != entity_id)
        .limit(1)
    )
    return str(other) if other is not None else None


def _problem(write: FileWrite, problem: str) -> FileWrite:
    return FileWrite(**{**write.__dict__, "problem": problem})


def _markdown_files(
    conn: Connection, schema: ThemeSchema, entity_id: int, entity_type: type
) -> list[tuple[int, str, str, int | None, int | None, bool]]:
    """The item's Markdown files (its primary role's), each with whether it is inside an
    archive: nothing writes into one (#391), and Write to file… says so (#524)."""
    primary = next((r.name for r in entity_type.roles if r.primary), None)  # type: ignore[attr-defined]
    query = (
        select(
            Resource.id,
            Resource.root_id,
            Resource.relpath,
            Resource.size,
            Resource.mtime_ns,
            Resource.ext,
            Resource.parent_resource_id,
        )
        .join(EntityResource, EntityResource.resource_id == Resource.id)
        .where(
            EntityResource.entity_id == entity_id,
            Resource.status == ResourceStatus.OK,
        )
        .order_by(Resource.relpath)
    )
    if primary is not None:
        query = query.where(EntityResource.role == primary)
    return [
        (rid, root, rel, size, mtime, parent is not None)
        for rid, root, rel, size, mtime, ext, parent in conn.execute(query)
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
    aliases: Mapping[str, str] | None = None,
    own: Mapping[str, tuple[str, Any]] | None = None,
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
        note = write.note is not None
        changes: dict[str, Any] = {
            **field_changes(theme, entity_type, record, edited, current, note, aliases)
        }
        changes.update(own_changes(current, own or {}))
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
    notes_write_back: bool = False,
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
        if not may_write(write, allowed, notes_write_back):
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
