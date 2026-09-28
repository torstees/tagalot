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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property

from sqlalchemy import Connection, Engine, delete, func, insert, literal, select, update

from tagalot.core.models import EntityTag, Tag, TagAlias

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
    match came through an alias rather than the name."""

    tag_id: int
    alias: str | None = None


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
                select(Tag.id, Tag.parent_id, Tag.name, Tag.color, Tag.sort_order)
            )
        ]
        aliases: dict[int, list[str]] = {}
        for tag_id, alias in conn.execute(select(TagAlias.tag_id, TagAlias.alias)):
            aliases.setdefault(tag_id, []).append(alias)
        return cls(nodes, aliases)

    def __len__(self) -> int:
        return len(self._nodes)

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
        """Tags whose name or an alias contains ``text``, ignoring case and accents (the
        filter box)."""
        needle = search_key(text)
        if not needle:
            return frozenset(self._nodes)
        return frozenset(
            tag_id
            for tag_id, node in self._nodes.items()
            if needle in search_key(node.name)
            or any(needle in search_key(a) for a in self._aliases.get(tag_id, ()))
        )

    def suggest(self, text: str, limit: int = 20) -> list["TagSuggestion"]:
        """Tags for an autocomplete box, best first (the filter bar's tag box, §12).

        A tag matches when its name or an alias contains ``text`` (ignoring case and
        accents). Matches
        rank: the whole name, the start of the name, the start of a word in it, anywhere in
        it, then the same four through an alias; ties go by name, then tree position. Blank
        text suggests nothing.
        """
        needle = search_key(text)
        if not needle:
            return []
        ranked: list[tuple[tuple[int, str, tuple[str, ...]], TagSuggestion]] = []
        for tag_id, node in self._nodes.items():
            rank = _match_rank(needle, node.name)
            alias: str | None = None
            if rank is None:
                alias_ranks = [
                    (r, a)
                    for a in self._aliases.get(tag_id, ())
                    if (r := _match_rank(needle, a)) is not None
                ]
                if not alias_ranks:
                    continue
                best, alias = min(alias_ranks, key=lambda ra: (ra[0], name_key(ra[1])))
                rank = best + 4
            key = (rank, name_key(node.name), tuple(name_key(n) for n in self.path(tag_id)))
            ranked.append((key, TagSuggestion(tag_id, alias)))
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
    the target, and the source is deleted.
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


def add_alias(conn: Connection, tag_id: int, alias: str) -> None:
    """Add an alternate name that matches the tag in the filter box."""
    tree = TagTree.load(conn)
    node = _existing(tree, tag_id)
    alias = clean_name(alias)
    if name_key(alias) == name_key(node.name):
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


def _merge(conn: Connection, tree: TagTree, source_id: int, target_id: int) -> None:
    source, target = tree.node(source_id), tree.node(target_id)
    copied = select(EntityTag.entity_id, literal(target_id), EntityTag.added_at).where(
        EntityTag.tag_id == source_id
    )
    conn.execute(
        insert(EntityTag)
        .from_select(["entity_id", "tag_id", "added_at"], copied)
        .prefix_with("OR IGNORE")  # entities that already have the target keep their row
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
