"""A keep's tag tree as YAML: export, and import with a preview (#395, DESIGN.md §7
"Importing and exporting tags").

The file is nested like the tree. A tag is written as a plain name with nothing under it
(``Read:``), as a short list of sub-tag names (``Mood: [Calm, Tense]``), or in the full
form: a mapping of ``icon``, ``color``, ``description``, ``aliases``, ``applies_to`` (kinds of item,
by the theme's names), and ``tags`` (its sub-tags, in any of the three forms). Export
always writes the full form, leaving out empty properties.

Import matches tags by path, ignoring case and accents (a name also matches an existing
tag's alias), creates the missing ones, and merges the file's properties into existing
tags: an icon, color, description, or ``applies_to`` replaces the tag's, aliases are added, and
anything the file leaves out stays. Optionally it deletes the tags the file doesn't name
that no item uses. :func:`plan_import` says what would happen; :func:`import_tags` does it
(through :meth:`~tagalot.core.tag_service.TagService.import_tags`, as one undoable step).

**Which items have each tag** (#396) can follow the tree, as a second YAML document (after
``---``) holding ``items:``: each item with the user's own tags, written with its kind,
title, key (its ``ingest_key``), primary files (relative to their watched folder), and tag
paths. Import finds each item by its key, else one of its files, else its kind and title
(when exactly one item has it), and puts the tags on it; optionally it also takes off the
user's tags the file doesn't list for it. Tags from files' keywords are never written: they
come back from the files.
"""

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlalchemy import Connection, delete, func, insert, select, update

from tagalot.core.keywords import forget_removals
from tagalot.core.models import (
    Entity,
    EntityResource,
    EntityTag,
    Resource,
    ResourceStatus,
    Tag,
    TagAlias,
)
from tagalot.core.search_fields import type_labels, type_plurals
from tagalot.core.tags import (
    MAX_DESCRIPTION,
    PATH_SEPARATOR,
    TagError,
    TagTree,
    clean_icon,
    clean_name,
    format_types,
    name_key,
    parse_types,
    scope_conflicts,
    search_key,
    set_tag_types,
    split_tag_path,
    tag_entities,
)
from tagalot.core.theme_schema import ThemeSchema

PROPERTIES = ("icon", "color", "description", "aliases", "applies_to", "tags")
"""The keys of a tag written in the full form."""
ITEMS = "items"
"""The key of the second document's list of tagged items (#396)."""
ITEM_KEYS = ("type", "title", "key", "files", "tags")
"""The keys of one item in that list."""
ITEM_PATH = " > "
"""How an item's tag paths are written (``Genre > Fantasy``); a path with a name holding
``>`` is written as a list of names instead."""

_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
HEADER = "# Tagalot tags. Edit this file and load it back with Tag manager → Import tags…\n"


@dataclass(frozen=True)
class Problem:
    """Something in the file that import leaves out; ``line`` counts from 1."""

    line: int | None
    message: str

    def __str__(self) -> str:
        return f"Line {self.line}: {self.message}" if self.line else self.message


@dataclass(frozen=True)
class TagUpdate:
    """An existing tag the import changes: its path, and which properties."""

    path: str
    changes: tuple[str, ...]


@dataclass(frozen=True)
class ImportPlan:
    """What importing a file would do (every list of tags is of display paths)."""

    new: tuple[str, ...] = ()
    updated: tuple[TagUpdate, ...] = ()
    deleted: tuple[str, ...] = ()
    """Tags the file doesn't name that no item uses (with the delete option)."""
    kept: tuple[str, ...] = ()
    """Tags the file doesn't name, kept because items use them (with the delete option)."""
    problems: tuple[Problem, ...] = ()
    removed_uses: int = 0
    """Uses taken off items because a tag's ``applies_to`` no longer allows them."""
    unreadable: bool = False
    """The file isn't YAML (or not text): nothing can be imported."""
    items: int = 0
    """Items the file lists that were found in this keep (#396)."""
    not_found: tuple[str, ...] = ()
    """Items the file lists that weren't, described ("Book “Small Gods”")."""
    tags_added: int = 0
    """Tags the import puts on items."""
    tags_removed: int = 0
    """The user's tags that matching the file would take off found items."""

    @property
    def changes(self) -> bool:
        return bool(self.new or self.updated or self.deleted or self.tags_added)


# --- export ---


class _Flow(list[str]):
    """A list written on one line: ``[SF, Sci-Fi]``."""


class _Dumper(yaml.SafeDumper):
    pass


def _flow(dumper: yaml.SafeDumper, data: _Flow) -> yaml.Node:
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


def _empty(dumper: yaml.SafeDumper, data: None) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:null", "")


def _text(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(_Flow, _flow)
_Dumper.add_representer(type(None), _empty)
_Dumper.add_representer(str, _text)


def _item_path(tree: TagTree, tag_id: int) -> str | _Flow:
    names = tree.path(tag_id)
    if any(">" in n or "\u203a" in n for n in names):
        return _Flow(names)
    return ITEM_PATH.join(names)


def tagged_items(conn: Connection, schema: ThemeSchema, tree: TagTree) -> list[dict[str, Any]]:
    """Every item with tags of the user's own (not its files'), as the export writes it:
    kind, title, key, primary files, and tag paths; by kind, then title (#396)."""
    tags: dict[int, set[int]] = defaultdict(set)
    rows = conn.execute(
        select(EntityTag.entity_id, EntityTag.tag_id).where(EntityTag.by_file.is_(False))
    )
    for entity_id, tag_id in rows:
        if tag_id in tree:
            tags[entity_id].add(tag_id)
    ids = sorted(tags)
    entities: dict[int, tuple[str, str, str | None]] = {}
    files: dict[int, list[str]] = defaultdict(list)
    primary = {
        t.type_id: next((r.name for r in t.entity.roles if r.primary), None)
        for t in schema.entities.values()
    }
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        found = conn.execute(
            select(Entity.id, Entity.type, Entity.title, Entity.ingest_key).where(
                Entity.id.in_(chunk)
            )
        )
        entities.update({r.id: (r.type, r.title, r.ingest_key) for r in found})
        linked = conn.execute(
            select(EntityResource.entity_id, EntityResource.role, Resource.relpath)
            .join(Resource, Resource.id == EntityResource.resource_id)
            .where(
                EntityResource.entity_id.in_(chunk),
                Resource.status != ResourceStatus.MISSING,
            )
            .order_by(EntityResource.sort_order, Resource.relpath)
        )
        for entity_id, role, relpath in linked:
            type_id = entities.get(entity_id, ("", "", None))[0]
            if role is not None and role == primary.get(type_id):
                files[entity_id].append(relpath)
    labels = type_labels(schema)
    written = []
    for entity_id in ids:
        if entity_id not in entities:
            continue
        type_id, title, key = entities[entity_id]
        item: dict[str, Any] = {"type": labels.get(type_id, type_id), "title": title}
        if key:
            item["key"] = key
        if files[entity_id]:
            item["files"] = _Flow(files[entity_id])
        paths = sorted(tags[entity_id], key=lambda t: [name_key(n) for n in tree.path(t)])
        item["tags"] = [_item_path(tree, t) for t in paths]
        written.append(item)
    written.sort(key=lambda i: (name_key(i["type"]), name_key(i["title"])))
    return written


def export_tags(
    tree: TagTree, schema: ThemeSchema, items: Sequence[dict[str, Any]] | None = None
) -> str:
    """The whole tree as YAML, every tag in the full form, in display order; with
    ``items`` (:func:`tagged_items`), followed by which items have each tag (#396)."""
    labels = type_labels(schema)

    def tag(tag_id: int) -> dict[str, Any] | None:
        node = tree.node(tag_id)
        data: dict[str, Any] = {}
        if node.icon:
            data["icon"] = node.icon
        if node.color:
            data["color"] = node.color
        if node.description:
            data["description"] = node.description
        if aliases := tree.aliases(tag_id):
            data["aliases"] = _Flow(sorted(aliases, key=name_key))
        if (types := parse_types(node.types)) is not None:
            data["applies_to"] = _Flow(sorted(labels.get(t, t) for t in types))
        if children := tree.children(tag_id):
            data["tags"] = {tree.node(c).name: tag(c) for c in children}
        return data or None

    data = {tree.node(t).name: tag(t) for t in tree.children(None)}
    body = yaml.dump(
        data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    if items is None:
        return HEADER + body
    listed = yaml.dump(
        {ITEMS: [_flowing_tags(i) for i in items]},
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,  # an item's tags on one line
    )
    return HEADER + body + ITEMS_HEADER + "---\n" + listed


def _flowing_tags(item: dict[str, Any]) -> dict[str, Any]:
    """An item with its tag paths written on one line, ``[A > B, C]``, unless one is a list
    of names."""
    tags = item["tags"]
    if all(isinstance(t, str) for t in tags):
        return {**item, "tags": _Flow(tags)}
    return item


ITEMS_HEADER = (
    "\n# Which items have each tag. Import finds each by its key, else a file, else its kind\n"
    "# and title.\n"
)


# --- reading the file ---


@dataclass
class _Parsed:
    """One tag as the file writes it; ``None`` and empty mean "not given"."""

    name: str
    line: int | None
    icon: str | None = None
    color: str | None = None
    description: str | None = None
    aliases: tuple[str, ...] = ()
    types: frozenset[str] | None = None
    children: list["_Parsed"] = field(default_factory=list)


def _key_line(data: Any, key: Any) -> int | None:
    try:
        return int(data.lc.key(key)[0]) + 1
    except (AttributeError, KeyError, TypeError, IndexError):
        return None


def _item_line(data: Any, index: int) -> int | None:
    try:
        return int(data.lc.item(index)[0]) + 1
    except (AttributeError, KeyError, TypeError, IndexError):
        return None


def _scalars(value: Any) -> list[Any] | None:
    """A property's values: a list, or one value; ``None`` if nested deeper."""
    items = value if isinstance(value, list) else [value]
    if any(isinstance(v, dict | list) for v in items):
        return None
    return [v for v in items if v is not None]


class _Reader:
    def __init__(self, schema: ThemeSchema) -> None:
        self.problems: list[Problem] = []
        self.items: list[_Item] = []
        """The items of the file's second document (#396)."""
        self.kinds: dict[str, str] = {}
        plurals = type_plurals(schema)
        for type_id, label in type_labels(schema).items():
            for name in (type_id, label, plurals[type_id]):
                self.kinds.setdefault(search_key(name), type_id)
        self.plurals = plurals

    def problem(self, line: int | None, message: str) -> None:
        self.problems.append(Problem(line, message))

    def read(self, text: str) -> list[_Parsed] | None:
        from ruamel.yaml import YAML
        from ruamel.yaml.error import MarkedYAMLError, YAMLError

        try:
            documents = list(YAML(typ="rt").load_all(text))
        except MarkedYAMLError as error:
            mark = error.problem_mark or error.context_mark
            line = mark.line + 1 if mark is not None else None
            self.problem(line, f"This isn't YAML Tagalot can read: {error.problem}.")
            return None
        except YAMLError as error:
            self.problem(None, f"This isn't YAML Tagalot can read: {error}.")
            return None
        if len(documents) == 1 and _is_items(documents[0]):
            documents.insert(0, None)  # only items: "---" before them starts the file
        data = documents[0] if documents else None
        if len(documents) > 1:
            self.read_items(documents[1])
        if len(documents) > 2:
            self.problem(None, "Only the first two parts (tags, then items) are read.")
        if data is None:
            return []
        if isinstance(data, dict):
            return self.mapping(data)
        if isinstance(data, list):
            return self.names(data, 1)
        self.problem(1, "The file should list tags, one per line, like “Genre:”.")
        return None

    def read_items(self, data: Any) -> None:
        """The second document: ``items:``, a list of tagged items."""
        if data is None:
            return
        listed = data.get(ITEMS) if isinstance(data, dict) else None
        if not isinstance(listed, list):
            self.problem(None, "After “---” the file should list “items:”.")
            return
        for index, entry in enumerate(listed):
            line = _item_line(listed, index)
            item = self.item(entry, line)
            if item is not None:
                self.items.append(item)

    def item(self, entry: Any, line: int | None) -> "_Item | None":
        if not isinstance(entry, dict):
            self.problem(line, "An item is written with its type, title, and tags.")
            return None
        unknown = [str(k) for k in entry if str(k) not in ITEM_KEYS]
        if unknown:
            self.problem(line, f"{unknown[0]!r} isn't an item key ({', '.join(ITEM_KEYS)}).")
        type_id = self.kinds.get(search_key(str(entry.get("type") or "")))
        title = str(entry.get("title") or "").strip()
        if type_id is None:
            kinds = ", ".join(sorted(self.plurals.values()))
            self.problem(
                line,
                f"{str(entry.get('type'))!r} isn't a kind of item in this keep ({kinds}): "
                "left out.",
            )
            return None
        if not title:
            self.problem(line, "An item without a title: left out.")
            return None
        files = _scalars(entry.get("files") or [])
        if files is None:
            self.problem(line, f"{title!r}: write its files as a list.")
            files = []
        tags: list[tuple[str, ...]] = []
        raw = entry.get("tags") or []
        for value in raw if isinstance(raw, list) else [raw]:
            try:
                if isinstance(value, list):
                    names = tuple(clean_name(str(n)) for n in value)
                else:
                    names = tuple(split_tag_path(str(value)))
            except TagError as error:
                self.problem(line, f"{title!r}: {error}")
                continue
            if names:
                tags.append(names)
        key = entry.get("key")
        return _Item(
            line,
            type_id,
            title,
            str(key) if key else None,
            tuple(str(f) for f in files),
            tuple(tags),
        )

    def mapping(self, data: Any) -> list[_Parsed]:
        found = []
        for key, value in data.items():
            tag = self.tag(key, value, _key_line(data, key))
            if tag is not None:
                found.append(tag)
        return self.unique(found)

    def names(self, data: Any, line: int | None) -> list[_Parsed]:
        found = []
        for index, item in enumerate(data):
            item_line = _item_line(data, index) or line
            if isinstance(item, dict | list):
                self.problem(
                    item_line,
                    "A short list of sub-tags holds only names. Write a sub-tag with its own "
                    "sub-tags or properties in the full form, under “tags:”.",
                )
                continue
            name = self.name(item, item_line)
            if name is not None:
                found.append(_Parsed(name, item_line))
        return self.unique(found)

    def name(self, raw: Any, line: int | None) -> str | None:
        if raw is None:
            self.problem(line, "A tag without a name.")
            return None
        try:
            return clean_name(str(raw))
        except TagError as error:
            self.problem(line, str(error))
            return None

    def unique(self, found: list[_Parsed]) -> list[_Parsed]:
        """Sibling names are unique ignoring case; a repeat is left out."""
        seen: set[str] = set()
        kept = []
        for tag in found:
            if name_key(tag.name) in seen:
                self.problem(tag.line, f"{tag.name!r} is here twice: the second is left out.")
                continue
            seen.add(name_key(tag.name))
            kept.append(tag)
        return kept

    def tag(self, key: Any, value: Any, line: int | None) -> _Parsed | None:
        name = self.name(key, line)
        if name is None:
            return None
        tag = _Parsed(name, line)
        if value is None:
            return tag
        if isinstance(value, list):
            tag.children = self.names(value, line)
            return tag
        if not isinstance(value, dict):
            self.problem(
                line,
                f"{name!r}: write its sub-tags as a list ([A, B]) or its properties under "
                f"it, not {str(value)!r}.",
            )
            return tag
        for prop, given in value.items():
            self.property(tag, str(prop), given, _key_line(value, prop) or line)
        return tag

    def property(self, tag: _Parsed, prop: str, value: Any, line: int | None) -> None:
        if prop not in PROPERTIES:
            self.problem(
                line,
                f"{prop!r} isn't a tag property ({', '.join(PROPERTIES)}); sub-tags go "
                "under “tags:”.",
            )
            return
        if value is None:
            return
        if prop == "tags":
            if isinstance(value, list):
                tag.children = self.names(value, line)
            elif isinstance(value, dict):
                tag.children = self.mapping(value)
            else:
                self.problem(line, f"{tag.name!r}: write its sub-tags under “tags:”.")
        elif prop == "icon":
            try:
                tag.icon = clean_icon(str(value))
            except TagError as error:
                self.problem(line, f"{str(value)!r}: {error}")
        elif prop == "color":
            if isinstance(value, str) and _COLOR.fullmatch(value.strip()):
                tag.color = value.strip().lower()
            else:
                self.problem(line, f'{str(value)!r} isn\'t a color: write one as "#rrggbb".')
        elif prop == "description":
            if isinstance(value, dict | list):
                self.problem(line, f"{tag.name!r}: a description is text.")
                return
            text = str(value).strip()
            if len(text) > MAX_DESCRIPTION:
                self.problem(line, f"A description can be at most {MAX_DESCRIPTION:,} characters.")
            elif text:
                tag.description = text
        elif prop == "aliases":
            self.aliases(tag, value, line)
        else:
            self.applies_to(tag, value, line)

    def aliases(self, tag: _Parsed, value: Any, line: int | None) -> None:
        items = _scalars(value)
        if items is None:
            self.problem(line, f"{tag.name!r}: write aliases as a list of names.")
            return
        aliases: dict[str, str] = {}
        for item in items:
            alias = self.name(item, line)
            if alias is not None and name_key(alias) != name_key(tag.name):
                aliases.setdefault(name_key(alias), alias)
        tag.aliases = tuple(aliases.values())

    def applies_to(self, tag: _Parsed, value: Any, line: int | None) -> None:
        items = _scalars(value)
        if items is None:
            self.problem(line, f"{tag.name!r}: write the kinds of item it applies to as a list.")
            return
        types = set()
        for item in items:
            type_id = self.kinds.get(search_key(str(item)))
            if type_id is None:
                kinds = ", ".join(sorted(self.plurals.values()))
                self.problem(
                    line,
                    f"{tag.name!r} applies to {str(item)!r}, which isn't a kind of item in "
                    f"this keep ({kinds}): left out.",
                )
            else:
                types.add(type_id)
        if types:
            tag.types = frozenset(types)


def _is_items(data: Any) -> bool:
    """A part of the file that is only ``items:``, listing mappings: the items, never tags
    (a tag's short list holds names only)."""
    if not isinstance(data, dict) or list(data) != [ITEMS]:
        return False
    listed = data[ITEMS]
    return isinstance(listed, list) and bool(listed) and all(isinstance(i, dict) for i in listed)


@dataclass(frozen=True)
class _Item:
    """One tagged item the file lists (#396)."""

    line: int | None
    type_id: str
    title: str
    key: str | None
    files: tuple[str, ...]
    tags: tuple[tuple[str, ...], ...]
    """Each tag's path, top first."""


# --- planning ---


@dataclass
class _Planned:
    """A file tag matched to an existing tag (``tag_id``) or new (``None``)."""

    parsed: _Parsed
    tag_id: int | None
    types: frozenset[str] | None
    """Its ``applies_to``, narrowed to its parent's; ``None`` when not given."""
    children: list["_Planned"]
    new_aliases: tuple[str, ...]
    rename: str | None = None
    """The file's spelling of a tag it names with different case or accents."""


def _match(tree: TagTree, parent_id: int | None, name: str) -> tuple[int | None, bool]:
    """The existing tag under ``parent_id`` that ``name`` names: by name ignoring case, then
    also ignoring accents, then by one of its aliases; and whether it was by its name."""
    children = tree.children(parent_id)
    key, loose = name_key(name), search_key(name)
    tests: tuple[Callable[[int], bool], ...] = (
        lambda c: name_key(tree.node(c).name) == key,
        lambda c: search_key(tree.node(c).name) == loose,
        lambda c: any(search_key(a) == loose for a in tree.aliases(c)),
    )
    for by_name, test in zip((True, True, False), tests, strict=True):
        for child in children:
            if test(child):
                return child, by_name
    return None, False


class _Planner:
    def __init__(self, tree: TagTree, reader: _Reader) -> None:
        self.tree = tree
        self.reader = reader
        self.new: list[str] = []
        self.updated: list[TagUpdate] = []
        self.named: set[int] = set()
        self.retyped: dict[int, frozenset[str]] = {}
        self.new_paths: set[tuple[str, ...]] = set()
        """The new tags' paths, each name by :func:`search_key`: an item's tag can name one."""

    def walk(
        self,
        parsed: Sequence[_Parsed],
        parent_id: int | None,
        parent_new: bool,
        inherited: frozenset[str] | None,
        path: tuple[str, ...],
    ) -> list[_Planned]:
        tree = self.tree
        planned = []
        for tag in parsed:
            tag_id, by_name = (None, False) if parent_new else _match(tree, parent_id, tag.name)
            if tag_id is not None and tag_id in self.named:
                self.reader.problem(
                    tag.line,
                    f"{tag.name!r} names the same tag as another one here "
                    f"({tree.node(tag_id).name!r}): left out.",
                )
                continue
            node = tree.node(tag_id) if tag_id is not None else None
            types = tag.types
            if types is not None and inherited is not None and not types <= inherited:
                wider = sorted(self.reader.plurals.get(t, t) for t in types - inherited)
                self.reader.problem(
                    tag.line,
                    f"{tag.name!r} applies to {', '.join(wider)}, which {path[-1]!r} doesn't: "
                    "left out.",
                )
                types = (types & inherited) or None
            own = types if types is not None else parse_types(node.types) if node else None
            scope = own if inherited is None else inherited if own is None else inherited & own
            rename = tag.name if node and by_name and tag.name != node.name else None
            here = (*path, node.name if node else tag.name)
            new_aliases = tag.aliases
            if node is None or tag_id is None:
                self.new.append(PATH_SEPARATOR.join(here))
                self.new_paths.add(tuple(search_key(n) for n in here))
            else:
                self.named.add(tag_id)
                known = {name_key(a) for a in tree.aliases(tag_id)} | {name_key(node.name)}
                new_aliases = tuple(a for a in tag.aliases if name_key(a) not in known)
                changes = [f"renamed to {rename!r}"] if rename else []
                if tag.icon is not None and tag.icon != node.icon:
                    changes.append("icon")
                if tag.color is not None and tag.color != node.color:
                    changes.append("color")
                if tag.description is not None and tag.description != node.description:
                    changes.append("description")
                if new_aliases:
                    changes.append("aliases")
                if types is not None and types != parse_types(node.types):
                    changes.append("applies to")
                    self.retyped[tag_id] = types
                if changes:
                    self.updated.append(TagUpdate(PATH_SEPARATOR.join(here), tuple(changes)))
            if rename:  # its sub-tags are shown under the new name
                here = (*path, rename)
            children = self.walk(tag.children, tag_id, tag_id is None, scope, here)
            planned.append(_Planned(tag, tag_id, types, children, new_aliases, rename))
        return planned


@dataclass(frozen=True)
class _Prepared:
    plan: ImportPlan
    planned: list[_Planned]
    doomed: frozenset[int]
    found: tuple[tuple[int, _Item], ...] = ()
    """The file's items found in the keep, with their entity ids (#396)."""


def _find(conn: Connection, item: _Item) -> int | None:
    """The item ``item`` names: by its key, else the one item of its kind with one of its
    files, else the one with its title (ignoring case); ``None`` when none or several."""
    of_type = Entity.type == item.type_id
    if item.key:
        found = conn.scalar(select(Entity.id).where(of_type, Entity.ingest_key == item.key))
        if found is not None:
            return int(found)
    if item.files:
        ids = set(
            conn.scalars(
                select(EntityResource.entity_id)
                .join(Resource, Resource.id == EntityResource.resource_id)
                .join(Entity, Entity.id == EntityResource.entity_id)
                .where(of_type, Resource.relpath.in_(item.files))
            )
        )
        if len(ids) == 1:
            return ids.pop()
    rows = conn.execute(
        select(Entity.id, Entity.title).where(
            of_type, func.lower(Entity.title) == item.title.lower()
        )
    ).all()
    same = [r.id for r in rows if r.title.casefold() == item.title.casefold()] or [
        r.id for r in rows
    ]
    return int(same[0]) if len(same) == 1 else None


def _resolve(tree: TagTree, names: Sequence[str]) -> int | None:
    """The tag at a path, matched level by level as import matches tags."""
    parent: int | None = None
    for name in names:
        parent, _ = _match(tree, parent, name)
        if parent is None:
            return None
    return parent


def _describe(item: _Item, labels: dict[str, str]) -> str:
    return f"{labels.get(item.type_id, item.type_id)} \u201c{item.title}\u201d"


def _plan_items(
    conn: Connection, schema: ThemeSchema, tree: TagTree, reader: _Reader, planner: _Planner
) -> tuple[list[tuple[int, _Item]], list[str], int, int]:
    """Find the file's items, and count the tags import would add and (to match the
    file) take off: ``(found, not found, added, removed)``."""
    labels = dict(type_labels(schema))
    found: list[tuple[int, _Item]] = []
    missing: list[str] = []
    wanted: dict[int, set[int]] = defaultdict(set)
    new_uses = 0
    types: dict[int, str] = {}
    for item in reader.items:
        entity_id = _find(conn, item)
        if entity_id is None:
            missing.append(_describe(item, labels))
            continue
        found.append((entity_id, item))
        types[entity_id] = item.type_id
        for names in item.tags:
            tag_id = _resolve(tree, names)
            if tag_id is not None:
                wanted[entity_id].add(tag_id)
            elif tuple(search_key(n) for n in names) in planner.new_paths:
                new_uses += 1
            else:
                reader.problem(
                    item.line,
                    f"{item.title!r}: the tag {ITEM_PATH.join(names)!r} isn't in this keep "
                    "or this file: left out.",
                )
    added, removed = new_uses, 0
    ids = sorted(wanted.keys() | {e for e, _ in found})
    have: dict[int, dict[int, bool]] = defaultdict(dict)
    for start in range(0, len(ids), 500):
        rows = conn.execute(
            select(EntityTag.entity_id, EntityTag.tag_id, EntityTag.by_file).where(
                EntityTag.entity_id.in_(ids[start : start + 500])
            )
        )
        for entity_id, tag_id, by_file in rows:
            have[entity_id][tag_id] = bool(by_file)
    for entity_id in ids:
        for tag_id in wanted[entity_id]:
            allowed = parse_types(tree.node(tag_id).types)
            if tag_id not in have[entity_id] and (allowed is None or types[entity_id] in allowed):
                added += 1
        removed += sum(
            1
            for t, by_file in have[entity_id].items()
            if not by_file and t not in wanted[entity_id]
        )
    return found, missing, added, removed


def _prepare(conn: Connection, text: str, schema: ThemeSchema, delete_unused: bool) -> _Prepared:
    tree = TagTree.load(conn)
    reader = _Reader(schema)
    parsed = reader.read(text)
    if parsed is None:
        return _Prepared(
            ImportPlan(problems=tuple(reader.problems), unreadable=True), [], frozenset()
        )
    planner = _Planner(tree, reader)
    planned = planner.walk(parsed, None, False, None, ())
    found, missing, added, removed_tags = _plan_items(conn, schema, tree, reader, planner)

    doomed: frozenset[int] = frozenset()
    deleted: list[str] = []
    kept_in_use: list[str] = []
    if delete_unused:
        used = set(conn.scalars(select(EntityTag.tag_id).distinct()))
        kept: set[int] = set()

        def keep(tag_id: int) -> bool:
            children = [keep(c) for c in tree.children(tag_id)]  # every child, to visit all
            if tag_id in planner.named or tag_id in used or any(children):
                kept.add(tag_id)
                if tag_id not in planner.named and tag_id in used:
                    kept_in_use.append(_path(tree, tag_id))
            return tag_id in kept

        for top in tree.children(None):
            keep(top)
        doomed = frozenset(set(tree) - kept)
        deleted = sorted((_path(tree, t) for t in doomed), key=name_key)

    retyped = tree
    for tag_id, types in planner.retyped.items():
        retyped = retyped.with_types(tag_id, types)
    removed: set[tuple[int, int]] = set()
    for tag_id in planner.retyped:
        removed.update(scope_conflicts(conn, retyped, tag_id))

    plan = ImportPlan(
        new=tuple(planner.new),
        updated=tuple(planner.updated),
        deleted=tuple(deleted),
        kept=tuple(sorted(kept_in_use, key=name_key)),
        problems=tuple(reader.problems),
        removed_uses=len(removed),
        items=len(found),
        not_found=tuple(missing),
        tags_added=added,
        tags_removed=removed_tags,
    )
    return _Prepared(plan, planned, doomed, tuple(found))


def _path(tree: TagTree, tag_id: int) -> str:
    return PATH_SEPARATOR.join(tree.path(tag_id))


def plan_import(
    conn: Connection, text: str, schema: ThemeSchema, delete_unused: bool = False
) -> ImportPlan:
    """What importing ``text`` would do to the keep's tags, without changing anything."""
    return _prepare(conn, text, schema, delete_unused).plan


def import_tags(
    conn: Connection,
    text: str,
    schema: ThemeSchema,
    delete_unused: bool = False,
    match_items: bool = False,
) -> ImportPlan:
    """Import ``text`` in one transaction (planned again against the tags as they are now);
    returns what it did. With ``match_items``, the user's tags on the file's items that it
    doesn't list for them are taken off (#396). A file that can't be read raises
    :class:`TagError`."""
    prepared = _prepare(conn, text, schema, delete_unused)
    if prepared.plan.unreadable:
        raise TagError(str(prepared.plan.problems[0]))
    tree = TagTree.load(conn)
    next_order: dict[int | None, int] = {}

    def order(parent_id: int | None) -> int:
        if parent_id not in next_order:
            existing = tree.children(parent_id) if parent_id is None or parent_id in tree else ()
            next_order[parent_id] = max((tree.node(c).sort_order for c in existing), default=-1) + 1
        next_order[parent_id] += 1
        return next_order[parent_id] - 1

    def apply(items: Iterable[_Planned], parent_id: int | None) -> None:
        for item in items:
            tag = item.parsed
            tag_id = item.tag_id
            if tag_id is None:
                tag_id = int(
                    conn.execute(
                        insert(Tag)
                        .values(
                            parent_id=parent_id,
                            name=tag.name,
                            icon=tag.icon,
                            color=tag.color,
                            description=tag.description,
                            types=format_types(item.types),
                            sort_order=order(parent_id),
                        )
                        .returning(Tag.id)
                    ).scalar_one()
                )
            else:
                values: dict[str, Any] = {}
                if item.rename is not None:
                    values["name"] = item.rename
                    same = [a for a in tree.aliases(tag_id) if name_key(a) == name_key(item.rename)]
                    if same:  # an alias that is now its name
                        conn.execute(
                            delete(TagAlias).where(
                                TagAlias.tag_id == tag_id, TagAlias.alias.in_(same)
                            )
                        )
                if tag.icon is not None:
                    values["icon"] = tag.icon
                if tag.color is not None:
                    values["color"] = tag.color
                if tag.description is not None:
                    values["description"] = tag.description
                if values:
                    conn.execute(update(Tag).where(Tag.id == tag_id).values(**values))
                if item.types is not None and item.types != parse_types(tree.node(tag_id).types):
                    set_tag_types(conn, tag_id, item.types)
            if item.new_aliases:
                conn.execute(
                    insert(TagAlias), [{"tag_id": tag_id, "alias": a} for a in item.new_aliases]
                )
            apply(item.children, tag_id)

    apply(prepared.planned, None)
    if prepared.doomed:
        conn.execute(delete(Tag).where(Tag.id.in_(prepared.doomed)))
    _tag_items(conn, prepared.found, match_items)
    return prepared.plan


def _tag_items(conn: Connection, found: Sequence[tuple[int, _Item]], match: bool) -> None:
    """Put the file's tags on the items it lists (the tree is imported by now, so every tag
    it names is there), and, to ``match``, take off the user's others (#396)."""
    if not found:
        return
    tree = TagTree.load(conn)
    wanted: dict[int, set[int]] = defaultdict(set)
    for entity_id, item in found:
        wanted[entity_id] |= {t for names in item.tags if (t := _resolve(tree, names))}
    by_tag: dict[int, set[int]] = defaultdict(set)
    for entity_id, tag_ids in wanted.items():
        for tag_id in tag_ids:
            by_tag[tag_id].add(entity_id)
    added: set[tuple[int, int]] = set()
    for tag_id, entity_ids in by_tag.items():
        added |= {(e, t) for e, t, _ in tag_entities(conn, entity_ids, [tag_id])}
    forget_removals(conn, added)  # put back by the user, as tagging by hand does
    if match:
        for entity_id, tag_ids in wanted.items():
            conn.execute(
                delete(EntityTag).where(
                    EntityTag.entity_id == entity_id,
                    EntityTag.by_file.is_(False),
                    EntityTag.tag_id.not_in(tag_ids),
                )
            )


def plan_summary(plan: ImportPlan) -> str:
    """A one-line account of an import, for the status bar."""
    parts = []
    for count, what in (
        (len(plan.new), "new"),
        (len(plan.updated), "updated"),
        (len(plan.deleted), "deleted"),
    ):
        if count:
            parts.append(f"{count:,} {what}")
    text = ", ".join(parts) if parts else "nothing to change"
    if plan.items:
        noun = "item" if plan.items == 1 else "items"
        text += f"; tagged {plan.items:,} {noun}"
    if plan.not_found:
        text += f", {len(plan.not_found):,} not found"
    return f"Imported tags: {text}."
