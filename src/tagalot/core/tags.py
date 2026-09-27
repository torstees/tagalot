"""Tag tree operations: add, rename, reparent, merge, delete.

The whole tree is small (hundreds to low thousands of tags), so it is loaded into an
immutable :class:`TagTree` snapshot and cached by :class:`TagTreeCache` until a tag operation
invalidates it (DESIGN.md §7).
"""

import logging
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property

from sqlalchemy import Connection, Engine, select

from tagalot.core.models import Tag, TagAlias

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


def _display_order(node: TagNode) -> tuple[int, str, int]:
    return (node.sort_order, name_key(node.name), node.id)


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
        """Tags whose name or an alias contains ``text``, ignoring case (the filter box)."""
        needle = name_key(text)
        if not needle:
            return frozenset(self._nodes)
        return frozenset(
            tag_id
            for tag_id, node in self._nodes.items()
            if needle in name_key(node.name)
            or any(needle in name_key(a) for a in self._aliases.get(tag_id, ()))
        )

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
