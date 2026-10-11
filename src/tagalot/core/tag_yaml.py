"""A keep's tag tree as YAML: export, and import with a preview (#395, DESIGN.md §7
"Importing and exporting tags").

The file is nested like the tree. A tag is written as a plain name with nothing under it
(``Read:``), as a short list of sub-tag names (``Mood: [Calm, Tense]``), or in the full
form: a mapping of ``color``, ``description``, ``aliases``, ``applies_to`` (kinds of item,
by the theme's names), and ``tags`` (its sub-tags, in any of the three forms). Export
always writes the full form, leaving out empty properties.

Import matches tags by path, ignoring case and accents (a name also matches an existing
tag's alias), creates the missing ones, and merges the file's properties into existing
tags: a color, description, or ``applies_to`` replaces the tag's, aliases are added, and
anything the file leaves out stays. Optionally it deletes the tags the file doesn't name
that no item uses. :func:`plan_import` says what would happen; :func:`import_tags` does it
(through :meth:`~tagalot.core.tag_service.TagService.import_tags`, as one undoable step).
"""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlalchemy import Connection, delete, insert, select, update

from tagalot.core.models import EntityTag, Tag, TagAlias
from tagalot.core.search_fields import type_labels, type_plurals
from tagalot.core.tags import (
    MAX_DESCRIPTION,
    PATH_SEPARATOR,
    TagError,
    TagTree,
    clean_name,
    format_types,
    name_key,
    parse_types,
    scope_conflicts,
    search_key,
    set_tag_types,
)
from tagalot.core.theme_schema import ThemeSchema

PROPERTIES = ("color", "description", "aliases", "applies_to", "tags")
"""The keys of a tag written in the full form."""

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

    @property
    def changes(self) -> bool:
        return bool(self.new or self.updated or self.deleted)


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


def export_tags(tree: TagTree, schema: ThemeSchema) -> str:
    """The whole tree as YAML, every tag in the full form, in display order."""
    labels = type_labels(schema)

    def tag(tag_id: int) -> dict[str, Any] | None:
        node = tree.node(tag_id)
        data: dict[str, Any] = {}
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
    return HEADER + body


# --- reading the file ---


@dataclass
class _Parsed:
    """One tag as the file writes it; ``None`` and empty mean "not given"."""

    name: str
    line: int | None
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
            data = YAML(typ="rt").load(text)
        except MarkedYAMLError as error:
            mark = error.problem_mark or error.context_mark
            line = mark.line + 1 if mark is not None else None
            self.problem(line, f"This isn't YAML Tagalot can read: {error.problem}.")
            return None
        except YAMLError as error:
            self.problem(None, f"This isn't YAML Tagalot can read: {error}.")
            return None
        if data is None:
            return []
        if isinstance(data, dict):
            return self.mapping(data)
        if isinstance(data, list):
            return self.names(data, 1)
        self.problem(1, "The file should list tags, one per line, like “Genre:”.")
        return None

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
            else:
                self.named.add(tag_id)
                known = {name_key(a) for a in tree.aliases(tag_id)} | {name_key(node.name)}
                new_aliases = tuple(a for a in tag.aliases if name_key(a) not in known)
                changes = [f"renamed to {rename!r}"] if rename else []
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
    )
    return _Prepared(plan, planned, doomed)


def _path(tree: TagTree, tag_id: int) -> str:
    return PATH_SEPARATOR.join(tree.path(tag_id))


def plan_import(
    conn: Connection, text: str, schema: ThemeSchema, delete_unused: bool = False
) -> ImportPlan:
    """What importing ``text`` would do to the keep's tags, without changing anything."""
    return _prepare(conn, text, schema, delete_unused).plan


def import_tags(
    conn: Connection, text: str, schema: ThemeSchema, delete_unused: bool = False
) -> ImportPlan:
    """Import ``text`` in one transaction (planned again against the tags as they are now);
    returns what it did. A file that can't be read raises :class:`TagError`."""
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
    return prepared.plan


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
    return f"Imported tags: {text}."
