"""Tag tree operations: add, rename, reparent, merge, delete.

The whole tree is small (hundreds to low thousands of tags), so it is loaded into an
immutable :class:`TagTree` snapshot and cached by :class:`TagTreeCache` until a tag operation
invalidates it (DESIGN.md §7).
"""

import enum
import logging
import re
import threading
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cached_property

from sqlalchemy import Connection, Engine, delete, func, insert, literal, select, tuple_, update

from tagalot.core.models import Entity, EntityTag, FileTagRemoval, Tag, TagAlias, utcnow

logger = logging.getLogger(__name__)

PATH_SEPARATOR = f" {chr(0x203A)} "  # U+203A SINGLE RIGHT-POINTING ANGLE QUOTATION MARK
"""Joins tag names in a path; the same separator as the breadcrumbs (DESIGN.md §12)."""


@dataclass(frozen=True)
class TagNode:
    """One tag as stored."""

    id: int
    parent_id: int | None
    name: str
    color: str | None
    sort_order: int
    description: str | None = None
    types: str | None = None
    """The item types it may be applied to, as stored (see :func:`parse_types`)."""


def parse_types(stored: str | None) -> frozenset[str] | None:
    """A tag's stored ``types`` as a set of type ids; ``None`` (every type) if unset."""
    if stored is None or not stored.split():
        return None
    return frozenset(stored.split())


def format_types(types: Iterable[str] | None) -> str | None:
    """Type ids as stored on a tag: sorted and space-separated; ``None`` for every type."""
    if types is None:
        return None
    ids = sorted(set(types))
    return " ".join(ids) if ids else None


def name_key(name: str) -> str:
    """How tag names compare: ignoring case in every script (``"Ärger" == "ärger"``)."""
    return name.strip().casefold()


def search_key(text: str) -> str:
    """How tag filters and suggestions match: ignoring case and accents, like the search box
    (``"isl"`` finds ``"Ísland"``). Names stay distinct by :func:`name_key`, so ``"Ärger"``
    and ``"Arger"`` can both exist."""
    decomposed = unicodedata.normalize("NFKD", name_key(text))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _display_order(node: TagNode) -> tuple[int, str, int]:
    return (node.sort_order, name_key(node.name), node.id)


@dataclass(frozen=True)
class TagSuggestion:
    """A tag offered for ``text`` by :meth:`TagTree.suggest`; ``alias`` is set when the
    match came through an alias rather than the name, ``in_description`` when it came only
    through the tag's description."""

    tag_id: int
    alias: str | None = None
    in_description: bool = False


_WORD_START = re.compile(r"\w+")


def _match_rank(needle: str, name: str) -> int | None:
    """0 whole name, 1 prefix, 2 start of a word, 3 anywhere; ``None`` if absent."""
    key = search_key(name)
    if needle not in key:
        return None
    if key == needle:
        return 0
    if key.startswith(needle):
        return 1
    if any(key.startswith(needle, m.start()) for m in _WORD_START.finditer(key)):
        return 2
    return 3


class TagTree:
    """An immutable snapshot of the tag tree with its aliases."""

    def __init__(self, nodes: Iterable[TagNode], aliases: Mapping[int, Iterable[str]]) -> None:
        self._nodes: dict[int, TagNode] = {n.id: n for n in nodes}
        self._aliases: dict[int, tuple[str, ...]] = {
            tag_id: tuple(names) for tag_id, names in aliases.items() if tag_id in self._nodes
        }
        children: dict[int | None, list[TagNode]] = {}
        for node in self._nodes.values():
            children.setdefault(node.parent_id, []).append(node)
        self._children: dict[int | None, tuple[int, ...]] = {
            parent: tuple(n.id for n in sorted(kids, key=_display_order))
            for parent, kids in children.items()
        }
        self._descendants: dict[int, frozenset[int]] = {}

    @classmethod
    def load(cls, conn: Connection) -> "TagTree":
        """Read the tree and its aliases from the database."""
        nodes = [
            TagNode(*row)
            for row in conn.execute(
                select(
                    Tag.id,
                    Tag.parent_id,
                    Tag.name,
                    Tag.color,
                    Tag.sort_order,
                    Tag.description,
                    Tag.types,
                )
            )
        ]
        aliases: dict[int, list[str]] = {}
        for tag_id, alias in conn.execute(select(TagAlias.tag_id, TagAlias.alias)):
            aliases.setdefault(tag_id, []).append(alias)
        return cls(nodes, aliases)

    def __len__(self) -> int:
        return len(self._nodes)

    def __iter__(self) -> Iterator[int]:
        """Every tag id (in no particular order)."""
        return iter(self._nodes)

    def __contains__(self, tag_id: object) -> bool:
        return tag_id in self._nodes

    def node(self, tag_id: int) -> TagNode:
        """The tag with this id; ``KeyError`` if there is none."""
        return self._nodes[tag_id]

    def aliases(self, tag_id: int) -> tuple[str, ...]:
        return self._aliases.get(tag_id, ())

    def children(self, parent_id: int | None = None) -> tuple[int, ...]:
        """Child ids in display order (``sort_order``, then name). ``None`` = top level."""
        if parent_id is not None and parent_id not in self._nodes:
            raise KeyError(parent_id)
        return self._children.get(parent_id, ())

    def find_child(self, parent_id: int | None, name: str) -> int | None:
        """The sibling under ``parent_id`` named ``name`` (case-insensitively), if any."""
        key = name_key(name)
        for child in self.children(parent_id):
            if name_key(self._nodes[child].name) == key:
                return child
        return None

    def ancestors(self, tag_id: int) -> tuple[int, ...]:
        """Ids from the top-level ancestor down to ``tag_id``'s parent."""
        chain: list[int] = []
        seen = {tag_id}
        parent = self.node(tag_id).parent_id
        while parent is not None:
            if parent in seen:  # impossible with valid data; don't hang on a corrupt tree
                logger.error("Tag %d has a cycle in its ancestors", tag_id)
                break
            seen.add(parent)
            chain.append(parent)
            parent = self._nodes[parent].parent_id
        return tuple(reversed(chain))

    def descendants(self, tag_id: int, *, include_self: bool = True) -> frozenset[int]:
        """``tag_id``'s whole subtree: what "this tag or any of its descendants" matches (§7)."""
        if tag_id not in self._descendants:
            self.node(tag_id)  # KeyError for unknown ids
            found: set[int] = set()
            stack = [tag_id]
            while stack:
                current = stack.pop()
                if current in found:
                    continue
                found.add(current)
                stack.extend(self._children.get(current, ()))
            self._descendants[tag_id] = frozenset(found)
        subtree = self._descendants[tag_id]
        return subtree if include_self else subtree - {tag_id}

    def scope(self, tag_id: int) -> frozenset[str] | None:
        """The item types ``tag_id`` may be applied to: what it and its ancestors allow
        (a sub-tag can only narrow its parent's), or ``None`` for every type (#135)."""
        allowed: frozenset[str] | None = None
        for t in (*self.ancestors(tag_id), tag_id):
            own = parse_types(self._nodes[t].types)
            if own is not None:
                allowed = own if allowed is None else allowed & own
        return allowed

    def allows(self, tag_id: int, type_id: str) -> bool:
        """Whether ``tag_id`` may be applied to an item of type ``type_id``."""
        allowed = self.scope(tag_id)
        return allowed is None or type_id in allowed

    def with_types(self, tag_id: int, types: Iterable[str] | None) -> "TagTree":
        """This tree with ``tag_id``'s own scope replaced (to preview a change)."""
        nodes = [
            replace(n, types=format_types(types)) if n.id == tag_id else n
            for n in self._nodes.values()
        ]
        return TagTree(nodes, self._aliases)

    def expand(self, tag_ids: Iterable[int]) -> frozenset[int]:
        """The union of several tags' subtrees (an include group or the exclude set, §8)."""
        result: frozenset[int] = frozenset()
        for tag_id in tag_ids:
            result |= self.descendants(tag_id)
        return result

    def path(self, tag_id: int) -> tuple[str, ...]:
        """Names from the top level down to ``tag_id``."""
        return tuple(self._nodes[t].name for t in (*self.ancestors(tag_id), tag_id))

    def display_name(self, tag_id: int) -> str:
        """The tag's name, or its full path when another tag has the same name (§5)."""
        name = self.node(tag_id).name
        if len(self._by_name.get(name_key(name), ())) > 1:
            return PATH_SEPARATOR.join(self.path(tag_id))
        return name

    def matching(self, text: str) -> frozenset[int]:
        """Tags whose name, an alias, or description contains ``text``, ignoring case and
        accents (the filter box)."""
        needle = search_key(text)
        if not needle:
            return frozenset(self._nodes)
        return frozenset(
            tag_id
            for tag_id, node in self._nodes.items()
            if needle in search_key(node.name)
            or any(needle in search_key(a) for a in self._aliases.get(tag_id, ()))
            or (node.description is not None and needle in search_key(node.description))
        )

    def suggest(self, text: str, limit: int = 20) -> list["TagSuggestion"]:
        """Tags for an autocomplete box, best first (the filter bar's tag box, §12).

        A tag matches when its name, an alias, or its description contains ``text``
        (ignoring case and accents). Matches
        rank: the whole name, the start of the name, the start of a word in it, anywhere in
        it, then the same four through an alias, then a match in the description; ties go
        by name, then tree position. Blank text suggests nothing.
        """
        needle = search_key(text)
        if not needle:
            return []
        ranked: list[tuple[tuple[int, str, tuple[str, ...]], TagSuggestion]] = []
        for tag_id, node in self._nodes.items():
            rank = _match_rank(needle, node.name)
            alias: str | None = None
            in_description = False
            if rank is None:
                alias_ranks = [
                    (r, a)
                    for a in self._aliases.get(tag_id, ())
                    if (r := _match_rank(needle, a)) is not None
                ]
                if alias_ranks:
                    best, alias = min(alias_ranks, key=lambda ra: (ra[0], name_key(ra[1])))
                    rank = best + 4
                elif node.description is not None and needle in search_key(node.description):
                    rank, in_description = 8, True
                else:
                    continue
            key = (rank, name_key(node.name), tuple(name_key(n) for n in self.path(tag_id)))
            ranked.append((key, TagSuggestion(tag_id, alias, in_description)))
        ranked.sort(key=lambda item: item[0])
        return [suggestion for _, suggestion in ranked[:limit]]

    def with_ancestors(self, tag_ids: Iterable[int]) -> frozenset[int]:
        """``tag_ids`` plus every ancestor, so filtered matches stay visible in the tree (§12)."""
        result: set[int] = set()
        for tag_id in tag_ids:
            result.add(tag_id)
            result.update(self.ancestors(tag_id))
        return frozenset(result)

    @cached_property
    def _by_name(self) -> dict[str, tuple[int, ...]]:
        by_name: dict[str, list[int]] = {}
        for node in self._nodes.values():
            by_name.setdefault(name_key(node.name), []).append(node.id)
        return {k: tuple(v) for k, v in by_name.items()}


class TagTreeCache:
    """Holds the current :class:`TagTree`, reloading it only after :meth:`invalidate`.

    Tag operations invalidate after they commit. Snapshots are immutable, so a reader holding
    an older tree is unaffected by a reload. Safe to use from several threads.
    """

    def __init__(self, reader: Engine) -> None:
        self._reader = reader
        self._lock = threading.Lock()
        self._tree: TagTree | None = None
        self._generation = 0

    def get(self) -> TagTree:
        """The current tree, loading it if it was invalidated."""
        with self._lock:
            if self._tree is None:
                with self._reader.connect() as conn:
                    self._tree = TagTree.load(conn)
                self._generation += 1
                logger.debug("Loaded tag tree (%d tags)", len(self._tree))
            return self._tree

    def invalidate(self) -> None:
        """Forget the cached tree; the next :meth:`get` reloads it."""
        with self._lock:
            self._tree = None

    @property
    def generation(self) -> int:
        """Increments on each reload, so views can tell whether their tree is current."""
        return self._generation


# --- Tag operations (DESIGN.md §7) ---
#
# Each operation takes a Connection and runs inside one DB-writer transaction. It reloads the
# tree from that connection, so validation never depends on a stale cache.

MAX_NAME_LENGTH = 200
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


class TagError(ValueError):
    """A tag operation was refused; the message is suitable for showing to the user."""


class DeleteMode(enum.Enum):
    """What to do with a deleted tag's children."""

    SUBTREE = "subtree"
    """Delete the children (and their descendants) too."""
    PROMOTE = "promote"
    """Move the children up to the deleted tag's parent."""


def clean_name(name: str) -> str:
    """Validate and normalize a tag name or alias (surrounding and repeated spaces removed)."""
    name = " ".join(name.split())
    if not name:
        raise TagError("A tag name can't be empty.")
    if len(name) > MAX_NAME_LENGTH:
        raise TagError(f"A tag name can be at most {MAX_NAME_LENGTH} characters.")
    return name


def add_tag(conn: Connection, parent_id: int | None, name: str, color: str | None = None) -> int:
    """Create a tag at the end of ``parent_id``'s children (``None`` = top level)."""
    tree = TagTree.load(conn)
    name = clean_name(name)
    if parent_id is not None and parent_id not in tree:
        raise TagError("The parent tag no longer exists.")
    _require_free_name(tree, parent_id, name)
    return int(
        conn.execute(
            insert(Tag)
            .values(
                parent_id=parent_id,
                name=name,
                color=_clean_color(color),
                sort_order=_next_sort_order(tree, parent_id),
            )
            .returning(Tag.id)
        ).scalar_one()
    )


_PATH_SPLIT = re.compile(rf"\s*{chr(0x203A)}\s*|\s+>\s+")
"""Where a typed tag path splits: at U+203A (the separator paths are shown with) or at ">"
with spaces around it, so names like "AC/DC" or "5>3" stay whole."""


def split_tag_path(text: str) -> list[str]:
    """The names in a typed tag path, top first: ``"Places > Norway"`` -> ``["Places",
    "Norway"]``. A text without a separator is one name. Each name is cleaned
    (:func:`clean_name`); an empty level is an error."""
    return [clean_name(part) for part in _PATH_SPLIT.split(text.strip())]


def add_tag_path(conn: Connection, names: Sequence[str]) -> int:
    """The tag at ``names`` (top first), creating it and any missing parents; returns its
    id. Existing levels are matched as siblings are (ignoring case)."""
    if not names:
        raise TagError("A tag name can't be empty.")
    parent: int | None = None
    for name in names:
        existing = TagTree.load(conn).find_child(parent, name)
        parent = existing if existing is not None else add_tag(conn, parent, name)
    assert parent is not None
    return parent


def rename_tag(conn: Connection, tag_id: int, name: str) -> None:
    """Rename a tag; the name must stay unique among its siblings (case-insensitively)."""
    tree = TagTree.load(conn)
    node = _existing(tree, tag_id)
    name = clean_name(name)
    _require_free_name(tree, node.parent_id, name, allow=tag_id)
    conn.execute(update(Tag).where(Tag.id == tag_id).values(name=name))


def reparent_tag(conn: Connection, tag_id: int, new_parent_id: int | None) -> None:
    """Move a tag (with its subtree) under ``new_parent_id``, at the end of its children."""
    tree = TagTree.load(conn)
    node = _existing(tree, tag_id)
    if new_parent_id == node.parent_id:
        return
    if new_parent_id is not None:
        _existing(tree, new_parent_id)
        if new_parent_id in tree.descendants(tag_id):
            raise TagError(f"Can't move {node.name!r} under itself or one of its own sub-tags.")
    _require_free_name(tree, new_parent_id, node.name)
    conn.execute(
        update(Tag)
        .where(Tag.id == tag_id)
        .values(parent_id=new_parent_id, sort_order=_next_sort_order(tree, new_parent_id))
    )


def merge_tags(conn: Connection, source_id: int, target_id: int) -> None:
    """Fold ``source`` into ``target`` (§7).

    Entities tagged with the source get the target (without duplicates). The source's
    children move under the target; a child whose name clashes with one of the target's
    children is merged into it the same way. The source's name and aliases become aliases of
    the target, and the source is deleted. A target without a description takes the
    source's.
    """
    tree = TagTree.load(conn)
    source, target = _existing(tree, source_id), _existing(tree, target_id)
    if source_id == target_id:
        raise TagError("Can't merge a tag into itself.")
    if target_id in tree.descendants(source_id):
        raise TagError(
            f"Can't merge {source.name!r} into one of its own sub-tags ({target.name!r})."
        )
    _merge(conn, tree, source_id, target_id)


def delete_tag(conn: Connection, tag_id: int, mode: DeleteMode | None = None) -> None:
    """Delete a tag. A tag with children needs a ``mode`` (§7).

    Entities lose the deleted tags; the entities themselves are untouched. Promoting children
    whose names clash with tags at the level above is refused rather than merged silently.
    """
    tree = TagTree.load(conn)
    node = _existing(tree, tag_id)
    children = tree.children(tag_id)
    if children and mode is None:
        raise TagError(f"{node.name!r} has sub-tags; choose to delete them or move them up.")
    if children and mode is DeleteMode.PROMOTE:
        clashes = []
        for child in children:
            other = tree.find_child(node.parent_id, tree.node(child).name)
            if other is not None and other != tag_id:
                clashes.append(repr(tree.node(child).name))
        if clashes:
            raise TagError(
                f"Can't move sub-tags up: the level above already has {', '.join(clashes)}. "
                "Rename or merge them first."
            )
        start = _next_sort_order(tree, node.parent_id)
        for offset, child in enumerate(children):
            conn.execute(
                update(Tag)
                .where(Tag.id == child)
                .values(parent_id=node.parent_id, sort_order=start + offset)
            )
        conn.execute(delete(Tag).where(Tag.id == tag_id))
    else:
        # One statement, so parent-child foreign keys are checked after every row is gone.
        conn.execute(delete(Tag).where(Tag.id.in_(tree.descendants(tag_id))))


def set_tag_color(conn: Connection, tag_id: int, color: str | None) -> None:
    """Set a tag's chip color (``#rrggbb``) or clear it with ``None``."""
    _existing(TagTree.load(conn), tag_id)
    conn.execute(update(Tag).where(Tag.id == tag_id).values(color=_clean_color(color)))


MAX_DESCRIPTION = 2000
"""Characters allowed in a tag description."""


def set_tag_description(conn: Connection, tag_id: int, description: str | None) -> None:
    """Set a tag's description (trimmed; blank clears it)."""
    _existing(TagTree.load(conn), tag_id)
    cleaned = (description or "").strip() or None
    if cleaned is not None and len(cleaned) > MAX_DESCRIPTION:
        raise TagError(f"A description can be at most {MAX_DESCRIPTION} characters.")
    conn.execute(update(Tag).where(Tag.id == tag_id).values(description=cleaned))


def scope_conflicts(conn: Connection, tree: TagTree, tag_id: int) -> list[tuple[int, int]]:
    """``(entity id, tag id)`` uses of ``tag_id`` and its sub-tags that ``tree`` doesn't
    allow (entities of other types): what limiting the tag's types would remove."""
    found: list[tuple[int, int]] = []
    for t in tree.descendants(tag_id):
        allowed = tree.scope(t)
        if allowed is None:
            continue
        rows = conn.execute(
            select(EntityTag.entity_id)
            .join(Entity, Entity.id == EntityTag.entity_id)
            .where(EntityTag.tag_id == t, Entity.type.not_in(allowed))
        )
        found.extend((e, t) for (e,) in rows)
    return found


def set_tag_types(conn: Connection, tag_id: int, types: Iterable[str] | None) -> int:
    """Limit ``tag_id`` (and its sub-tags) to these item types, or ``None`` for every type
    (#135), and take it off the items it no longer allows. Returns how many uses were
    removed. The UI asks first (:func:`scope_conflicts`)."""
    tree = TagTree.load(conn)
    _existing(tree, tag_id)
    stored = format_types(types)
    if types is not None and stored is None:
        raise TagError("Choose at least one type, or let the tag apply to every type.")
    conn.execute(update(Tag).where(Tag.id == tag_id).values(types=stored))
    conflicts = scope_conflicts(conn, tree.with_types(tag_id, types), tag_id)
    for chunk in range(0, len(conflicts), LINK_BATCH):
        pairs = conflicts[chunk : chunk + LINK_BATCH]
        conn.execute(
            delete(EntityTag).where(tuple_(EntityTag.entity_id, EntityTag.tag_id).in_(pairs))
        )
    return len(conflicts)


def description_excerpt(description: str, text: str, width: int = 40) -> str:
    """A short piece of ``description`` around the first match of ``text`` (ignoring case
    and accents), with ellipses where it was cut; the start of it if ``text`` isn't found."""
    flat = " ".join(description.split())
    if len(flat) <= width:
        return flat
    needle = search_key(text)
    position = next(
        (
            i
            for i in range(len(flat))
            if needle and search_key(flat[i : i + len(needle) + 4]).startswith(needle)
        ),
        0,
    )
    start = max(0, min(position - width // 4, len(flat) - width))
    end = min(len(flat), start + width)
    # Cut at word boundaries, never inside the match.
    if start > 0 and (space := flat.find(" ", start, position)) != -1:
        start = space + 1
    if end < len(flat) and (space := flat.rfind(" ", position + len(text), end)) != -1:
        end = space
    return ("…" if start > 0 else "") + flat[start:end] + ("…" if end < len(flat) else "")


def add_alias(conn: Connection, tag_id: int, alias: str, *, allow_name: bool = False) -> None:
    """Add an alternate name that matches the tag in the filter box (and file keywords,
    §7). ``allow_name`` lets the alias be the tag's own name: mapping the keyword "Dark" to
    one of two tags named Dark (#295)."""
    tree = TagTree.load(conn)
    node = _existing(tree, tag_id)
    alias = clean_name(alias)
    if name_key(alias) == name_key(node.name) and not allow_name:
        raise TagError("An alias must differ from the tag's name.")
    if any(name_key(a) == name_key(alias) for a in tree.aliases(tag_id)):
        return
    conn.execute(insert(TagAlias).values(tag_id=tag_id, alias=alias))


def remove_alias(conn: Connection, tag_id: int, alias: str) -> None:
    """Remove an alias (matched case-insensitively)."""
    tree = TagTree.load(conn)
    _existing(tree, tag_id)
    doomed = [a for a in tree.aliases(tag_id) if name_key(a) == name_key(alias)]
    if doomed:
        conn.execute(delete(TagAlias).where(TagAlias.tag_id == tag_id, TagAlias.alias.in_(doomed)))


def count_tagged_entities(conn: Connection, tag_ids: Iterable[int]) -> int:
    """How many distinct entities carry any of ``tag_ids`` directly (for confirmations)."""
    ids = list(tag_ids)
    if not ids:
        return 0
    query = select(func.count(func.distinct(EntityTag.entity_id))).where(EntityTag.tag_id.in_(ids))
    return int(conn.scalar(query) or 0)


def subtree_usage(conn: Connection, tag_id: int) -> int:
    """Entities tagged with ``tag_id`` or a descendant: the count §7 shows before a reparent
    or delete."""
    return count_tagged_entities(conn, TagTree.load(conn).descendants(tag_id))


# --- Tagging entities ---

LINK_BATCH = 500
"""Entities per statement when tagging a selection (SQLite limits bound parameters)."""

LinkRow = tuple[int, int, datetime]
"""An ``entity_tag`` row: ``(entity_id, tag_id, added_at)``."""


def tag_entities(
    conn: Connection,
    entity_ids: Iterable[int],
    tag_ids: Iterable[int],
    skipped: list[tuple[int, int, str]] | None = None,
) -> frozenset[LinkRow]:
    """Apply every tag in ``tag_ids`` to every entity in ``entity_ids``; returns the rows
    added. Pairs that already exist are left alone, entities that no longer exist are
    skipped, and a tag that no longer exists is an error. Parents are not added (§7).

    A tag limited to some item types (#135) isn't applied to entities of other types; those
    pairs are added to ``skipped`` as ``(entity id, tag id, entity type)``, if given."""
    tree = TagTree.load(conn)
    tags = _existing_tags(conn, tag_ids, tree)
    now = utcnow()
    added: set[LinkRow] = set()
    for chunk in _batches(entity_ids):
        found = conn.execute(select(Entity.id, Entity.type).where(Entity.id.in_(chunk)))
        present: dict[int, str] = {e: t for e, t in found}
        pairs = conn.execute(
            select(EntityTag.entity_id, EntityTag.tag_id).where(
                EntityTag.entity_id.in_(chunk), EntityTag.tag_id.in_(tags)
            )
        )
        have = {(e, t) for e, t in pairs}
        new = []
        for e in chunk:
            if e not in present:
                continue
            for t in tags:
                if (e, t) in have:
                    continue
                if tree.allows(t, present[e]):
                    new.append((e, t, now))
                elif skipped is not None:
                    skipped.append((e, t, present[e]))
        if new:
            conn.execute(
                insert(EntityTag),
                [{"entity_id": e, "tag_id": t, "added_at": a} for e, t, a in new],
            )
            added.update(new)
    return frozenset(added)


def untag_entities(
    conn: Connection, entity_ids: Iterable[int], tag_ids: Iterable[int]
) -> frozenset[LinkRow]:
    """Remove every tag in ``tag_ids`` from every entity in ``entity_ids``; returns the rows
    removed. Only these exact tags are removed, never their descendants."""
    tags = _existing_tags(conn, tag_ids)
    removed: set[LinkRow] = set()
    for chunk in _batches(entity_ids):
        where = (EntityTag.entity_id.in_(chunk), EntityTag.tag_id.in_(tags))
        rows = conn.execute(
            select(EntityTag.entity_id, EntityTag.tag_id, EntityTag.added_at).where(*where)
        )
        found = {(e, t, a) for e, t, a in rows}
        if found:
            conn.execute(delete(EntityTag).where(*where))
            removed.update(found)
    return frozenset(removed)


@dataclass(frozen=True)
class TagUsage:
    """How many entities use a tag: directly, and with any of its sub-tags (each entity
    counted once)."""

    direct: int
    with_subtags: int


def tag_usage(conn: Connection, tree: TagTree) -> dict[int, TagUsage]:
    """Usage counts for every tag in ``tree`` (the tag manager's columns, §12). One grouped
    query for direct uses, then one distinct count per tag that has sub-tags."""
    direct: dict[int, int] = {
        tag_id: int(count)
        for tag_id, count in conn.execute(
            select(EntityTag.tag_id, func.count()).group_by(EntityTag.tag_id)
        )
    }
    usage: dict[int, TagUsage] = {}
    for tag_id in tree:
        own = direct.get(tag_id, 0)
        if not tree.children(tag_id):
            usage[tag_id] = TagUsage(own, own)
            continue
        subtree = tree.descendants(tag_id)
        total = conn.scalar(
            select(func.count(func.distinct(EntityTag.entity_id))).where(
                EntityTag.tag_id.in_(subtree)
            )
        )
        usage[tag_id] = TagUsage(own, int(total or 0))
    return usage


def tag_counts(conn: Connection, entity_ids: Iterable[int]) -> dict[int, int]:
    """For each tag, how many of ``entity_ids`` carry it directly (the tagging panel's
    all / some / none summary); tags on none of them are absent."""
    counts: dict[int, int] = {}
    for chunk in _batches(entity_ids):
        rows = conn.execute(
            select(EntityTag.tag_id, func.count())
            .where(EntityTag.entity_id.in_(chunk))
            .group_by(EntityTag.tag_id)
        )
        for tag_id, count in rows:
            counts[tag_id] = counts.get(tag_id, 0) + int(count)
    return counts


def entity_types(conn: Connection, entity_ids: Iterable[int]) -> frozenset[str]:
    """The types of these entities (which tags the tagging panel offers them, #135)."""
    found: set[str] = set()
    for chunk in _batches(entity_ids):
        found.update(conn.scalars(select(Entity.type).where(Entity.id.in_(chunk)).distinct()))
    return frozenset(found)


def entity_tags(conn: Connection, entity_ids: Iterable[int]) -> dict[int, list[int]]:
    """The tags applied directly to each entity (entities without tags are absent)."""
    found: dict[int, list[int]] = {}
    for chunk in _batches(entity_ids):
        rows = conn.execute(
            select(EntityTag.entity_id, EntityTag.tag_id).where(EntityTag.entity_id.in_(chunk))
        )
        for entity_id, tag_id in rows:
            found.setdefault(entity_id, []).append(tag_id)
    return found


def _existing_tags(
    conn: Connection, tag_ids: Iterable[int], tree: TagTree | None = None
) -> list[int]:
    tree = tree or TagTree.load(conn)
    tags = sorted(set(tag_ids))
    for tag_id in tags:
        _existing(tree, tag_id)
    return tags


def _batches(ids: Iterable[int]) -> list[list[int]]:
    unique = sorted(set(ids))
    return [unique[i : i + LINK_BATCH] for i in range(0, len(unique), LINK_BATCH)]


def _merge(conn: Connection, tree: TagTree, source_id: int, target_id: int) -> None:
    source, target = tree.node(source_id), tree.node(target_id)
    copied = select(
        EntityTag.entity_id, literal(target_id), EntityTag.added_at, EntityTag.by_file
    ).where(EntityTag.tag_id == source_id)
    conn.execute(
        insert(EntityTag)
        .from_select(["entity_id", "tag_id", "added_at", "by_file"], copied)
        .prefix_with("OR IGNORE")  # entities that already have the target keep their row
    )
    removed = select(FileTagRemoval.entity_id, literal(target_id)).where(
        FileTagRemoval.tag_id == source_id
    )
    conn.execute(  # a file tag removed by hand stays removed under its new name (#294)
        insert(FileTagRemoval)
        .from_select(["entity_id", "tag_id"], removed)
        .prefix_with("OR IGNORE")
    )
    start = _next_sort_order(tree, target_id)
    for offset, child in enumerate(tree.children(source_id)):
        clash = tree.find_child(target_id, tree.node(child).name)
        if clash is not None:
            _merge(conn, tree, child, clash)
        else:
            conn.execute(
                update(Tag)
                .where(Tag.id == child)
                .values(parent_id=target_id, sort_order=start + offset)
            )
    known = {name_key(target.name)} | {name_key(a) for a in tree.aliases(target_id)}
    for alias in (source.name, *tree.aliases(source_id)):
        if name_key(alias) not in known:
            known.add(name_key(alias))
            conn.execute(insert(TagAlias).values(tag_id=target_id, alias=alias))
    if target.description is None and source.description is not None:
        conn.execute(update(Tag).where(Tag.id == target_id).values(description=source.description))
    conn.execute(delete(Tag).where(Tag.id == source_id))  # its entity_tag rows cascade


def _existing(tree: TagTree, tag_id: int) -> TagNode:
    if tag_id not in tree:
        raise TagError("That tag no longer exists.")
    return tree.node(tag_id)


def _require_free_name(
    tree: TagTree, parent_id: int | None, name: str, allow: int | None = None
) -> None:
    clash = tree.find_child(parent_id, name)
    if clash is not None and clash != allow:
        where = f"under {tree.node(parent_id).name!r}" if parent_id is not None else "at the top"
        raise TagError(f"There is already a tag named {tree.node(clash).name!r} {where}.")


def _next_sort_order(tree: TagTree, parent_id: int | None) -> int:
    return max((tree.node(c).sort_order for c in tree.children(parent_id)), default=-1) + 1


def _clean_color(color: str | None) -> str | None:
    if color is None:
        return None
    if not _COLOR.fullmatch(color):
        raise TagError(f"Colors are written as #rrggbb, not {color!r}.")
    return color.lower()
