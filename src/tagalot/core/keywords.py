"""File keywords: files' own tags becoming Tagalot tags (DESIGN.md §7 "File keywords", #294).

Themes report what a file says about an item (``ctx.keywords``: front matter's tags, EPUB
subjects); those keywords are stored per item and file (``entity_keyword``). A keyword
becomes a tag only by matching one the user defined (:func:`keyword_index`): its full path
(``Genre/Fantasy``), an alias, or its name when no other tag has that name, ignoring case
and accents. Nothing here ever creates a tag.

The tags keywords give are **derived**: rows in ``entity_tag`` marked ``by_file``, recomputed
by :func:`apply_file_tags` from the keywords, the tag tree (paths, aliases, types), and what
the user removed by hand (``file_tag_removal``). Scans call it for the items they read; tag
operations and their undo call it for every item, since a rename or a new alias changes what
matches. Undo history records only the user's own rows, so a recompute is all a file tag
ever needs.
"""

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import Connection, delete, distinct, func, insert, select, tuple_

from tagalot.core.models import (
    Entity,
    EntityKeyword,
    EntityTag,
    FileTagRemoval,
    KeywordIgnored,
    utcnow,
)
from tagalot.core.tags import TagTree, search_key

logger = logging.getLogger(__name__)

BATCH = 500
_LEVELS = re.compile(r"\s*(?:/|>|\u203a)\s*")
"""Path separators a keyword may use: ``/`` (as Obsidian writes nested tags), ``>``, and
Tagalot's own right-pointing angle quote (U+203A, ``PATH_SEPARATOR``)."""


def keyword_key(text: str) -> str:
    """How keywords and tags are compared: each level ignoring case and accents, levels
    joined by ``/``: ``"genre/fantasy"``, ``"Genre > Fantasy"``, and the path as Tagalot
    writes it are the same key. Empty for a keyword with no words."""
    levels = (search_key(level) for level in _LEVELS.split(text.strip()))
    return "/".join(level for level in levels if level)


def keyword_index(tree: TagTree) -> dict[str, int]:
    """``{keyword key: tag id}`` for every key that names exactly one tag: full paths first,
    then aliases, then names no other tag shares. An alias or a name two tags share matches
    neither."""
    by_alias: dict[str, set[int]] = {}
    by_name: dict[str, set[int]] = {}
    paths: dict[str, int] = {}
    for tag_id in tree:
        paths["/".join(keyword_key(name) for name in tree.path(tag_id))] = tag_id
        by_name.setdefault(keyword_key(tree.node(tag_id).name), set()).add(tag_id)
        for alias in tree.aliases(tag_id):
            by_alias.setdefault(keyword_key(alias), set()).add(tag_id)
    index = {key: next(iter(ids)) for key, ids in by_name.items() if key and len(ids) == 1}
    index.update({key: next(iter(ids)) for key, ids in by_alias.items() if key and len(ids) == 1})
    index.update(paths)
    return index


def set_keywords(
    conn: Connection, entity_id: int, resource_id: int, keywords: Iterable[object]
) -> bool:
    """Replace what one file says about one item; returns whether it changed. Keywords that
    are the same once compared (``"Fantasy"``, ``"fantasy"``) are kept once, as first given."""
    wanted: dict[str, str] = {}
    for keyword in keywords:
        if not isinstance(keyword, str):
            continue
        cleaned = " ".join(keyword.split())
        key = keyword_key(cleaned)
        if key and key not in wanted:
            wanted[key] = cleaned
    where = (EntityKeyword.entity_id == entity_id, EntityKeyword.resource_id == resource_id)
    current = {
        k: w
        for k, w in conn.execute(
            select(EntityKeyword.match_key, EntityKeyword.keyword).where(*where)
        )
    }
    if current == wanted:
        return False
    conn.execute(delete(EntityKeyword).where(*where))
    if wanted:
        conn.execute(
            insert(EntityKeyword),
            [
                {"entity_id": entity_id, "resource_id": resource_id, "match_key": k, "keyword": w}
                for k, w in wanted.items()
            ],
        )
    return True


def file_tags(
    conn: Connection, tree: TagTree, entity_ids: Iterable[int] | None = None
) -> dict[int, set[int]]:
    """``{entity: tags its keywords give it}``: keywords matching a tag that allows the
    item's type, less the ones the user removed. ``None`` reads every item with keywords."""
    index = keyword_index(tree)
    found: dict[int, set[int]] = {}
    query = select(EntityKeyword.entity_id, EntityKeyword.match_key, Entity.type).join(
        Entity, Entity.id == EntityKeyword.entity_id
    )
    for chunk in _chunks(entity_ids):
        rows = conn.execute(
            query if chunk is None else query.where(EntityKeyword.entity_id.in_(chunk))
        )
        for entity_id, key, type_id in rows:
            tag_id = index.get(key)
            if tag_id is not None and tree.allows(tag_id, type_id):
                found.setdefault(entity_id, set()).add(tag_id)
    removals = select(FileTagRemoval.entity_id, FileTagRemoval.tag_id)
    for chunk in _chunks(entity_ids):
        removed = conn.execute(
            removals if chunk is None else removals.where(FileTagRemoval.entity_id.in_(chunk))
        )
        for entity_id, tag_id in removed:
            found.get(entity_id, set()).discard(tag_id)
    return found


@dataclass(frozen=True)
class FileTagChange:
    added: int = 0
    removed: int = 0


def apply_file_tags(
    conn: Connection, entity_ids: Iterable[int] | None = None, tree: TagTree | None = None
) -> FileTagChange:
    """Bring file tags (``by_file`` rows) in step with keywords, for these items or every item
    (``None``). The user's own rows are never touched; a file tag an item already has by hand
    stays the user's."""
    tree = tree or TagTree.load(conn)
    ids = None if entity_ids is None else sorted(set(entity_ids))
    wanted = file_tags(conn, tree, ids)
    have: dict[tuple[int, int], bool] = {}
    query = select(EntityTag.entity_id, EntityTag.tag_id, EntityTag.by_file)
    for chunk in _chunks(ids):
        rows = conn.execute(
            query.where(EntityTag.by_file.is_(True))
            if chunk is None
            else query.where(EntityTag.entity_id.in_(chunk))
        )
        have.update({(e, t): bool(f) for e, t, f in rows})
    if ids is None and wanted:  # the user's rows for items with keywords, to leave alone
        for part in _chunks(sorted(wanted)):
            assert part is not None
            own = conn.execute(query.where(EntityTag.entity_id.in_(part)))
            have.update({(e, t): bool(f) for e, t, f in own})
    add = [(e, t) for e, tags in wanted.items() for t in tags if (e, t) not in have]
    remove = [(e, t) for (e, t), by_file in have.items() if by_file and t not in wanted.get(e, ())]
    now = utcnow()
    for start in range(0, len(add), BATCH):
        conn.execute(
            insert(EntityTag),
            [
                {"entity_id": e, "tag_id": t, "added_at": now, "by_file": True}
                for e, t in add[start : start + BATCH]
            ],
        )
    for start in range(0, len(remove), BATCH):
        pairs = remove[start : start + BATCH]
        conn.execute(
            delete(EntityTag).where(
                tuple_(EntityTag.entity_id, EntityTag.tag_id).in_(pairs),
                EntityTag.by_file.is_(True),
            )
        )
    if add or remove:
        logger.debug("File tags: %d added, %d removed", len(add), len(remove))
    return FileTagChange(len(add), len(remove))


def remember_removals(
    conn: Connection, tree: TagTree, pairs: Iterable[tuple[int, int]]
) -> frozenset[tuple[int, int]]:
    """The user took these ``(entity, tag)`` pairs off by hand: for those an item's keywords
    give, remember it so scans leave the tag off. Returns the records added."""
    pairs = set(pairs)
    if not pairs:
        return frozenset()
    given = file_tags(conn, tree, {e for e, _ in pairs})
    existing = _removals(conn, {e for e, _ in pairs})
    wanted = frozenset(p for p in pairs if p[1] in given.get(p[0], ()) and p not in existing)
    if wanted:
        conn.execute(insert(FileTagRemoval), [{"entity_id": e, "tag_id": t} for e, t in wanted])
    return wanted


def forget_removals(
    conn: Connection, pairs: Iterable[tuple[int, int]]
) -> frozenset[tuple[int, int]]:
    """The user put these pairs back by hand: drop any removal records. Returns those
    dropped."""
    pairs = set(pairs)
    if not pairs:
        return frozenset()
    found = frozenset(p for p in _removals(conn, {e for e, _ in pairs}) if p in pairs)
    for start in range(0, len(found), BATCH):
        chunk = sorted(found)[start : start + BATCH]
        conn.execute(
            delete(FileTagRemoval).where(
                tuple_(FileTagRemoval.entity_id, FileTagRemoval.tag_id).in_(chunk)
            )
        )
    return found


def _removals(conn: Connection, entity_ids: set[int]) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    for chunk in _chunks(sorted(entity_ids)):
        assert chunk is not None
        rows = conn.execute(
            select(FileTagRemoval.entity_id, FileTagRemoval.tag_id).where(
                FileTagRemoval.entity_id.in_(chunk)
            )
        )
        found.update((e, t) for e, t in rows)
    return found


def _chunks(ids: Iterable[int] | None) -> list[list[int] | None]:
    """``ids`` in statement-sized pieces; ``[None]`` (one pass over everything) for ``None``."""
    if ids is None:
        return [None]
    unique = sorted(set(ids))
    return [unique[i : i + BATCH] for i in range(0, len(unique), BATCH)]


def keywords_of(conn: Connection, entity_ids: Iterable[int]) -> Mapping[int, list[str]]:
    """Each item's keywords, as its files wrote them (the first spelling of each), in
    alphabetical order."""
    found: dict[int, dict[str, str]] = {}
    for chunk in _chunks(entity_ids):
        assert chunk is not None
        rows = conn.execute(
            select(EntityKeyword.entity_id, EntityKeyword.match_key, EntityKeyword.keyword)
            .where(EntityKeyword.entity_id.in_(chunk))
            .order_by(EntityKeyword.resource_id)
        )
        for entity_id, key, keyword in rows:
            found.setdefault(entity_id, {}).setdefault(key, keyword)
    return {e: list(words.values()) for e, words in found.items()}


# --- the File keywords page (#295) ---


@dataclass(frozen=True)
class KeywordInfo:
    """One keyword across the keep, for the File keywords page."""

    key: str
    keyword: str
    """How a file wrote it (one of its spellings)."""
    items: int
    """Items whose files give it."""
    examples: tuple[str, ...]
    """A few of those items' titles."""
    tag_id: int | None
    """The tag it matches, or ``None``."""
    ignored: bool


def keyword_report(conn: Connection, tree: TagTree, examples: int = 3) -> list[KeywordInfo]:
    """Every keyword files give, most-used first, with what it matches."""
    index = keyword_index(tree)
    ignored = set(conn.scalars(select(KeywordIgnored.match_key)))
    counts = conn.execute(
        select(
            EntityKeyword.match_key,
            func.count(distinct(EntityKeyword.entity_id)),
            func.min(EntityKeyword.keyword),
        ).group_by(EntityKeyword.match_key)
    ).all()
    ranked = (
        select(
            EntityKeyword.match_key,
            Entity.title,
            func.row_number()
            .over(partition_by=EntityKeyword.match_key, order_by=Entity.title)
            .label("n"),
        )
        .join(Entity, Entity.id == EntityKeyword.entity_id)
        .distinct()
        .subquery()
    )
    titles: dict[str, list[str]] = {}
    for key, title in conn.execute(
        select(ranked.c.match_key, ranked.c.title).where(ranked.c.n <= examples)
    ):
        if title not in titles.setdefault(key, []):
            titles[key].append(title)
    report = [
        KeywordInfo(
            key, keyword, int(n), tuple(titles.get(key, ())), index.get(key), key in ignored
        )
        for key, n, keyword in counts
    ]
    report.sort(key=lambda k: (-k.items, k.keyword.casefold()))
    return report


def unmatched_keys(conn: Connection, tree: TagTree) -> frozenset[str]:
    """Keywords that match no tag and aren't ignored (the TOOLS count)."""
    return frozenset(
        k.key for k in keyword_report(conn, tree, examples=0) if k.tag_id is None and not k.ignored
    )


@dataclass(frozen=True)
class IgnoreChange:
    """Ignoring or no longer ignoring keywords (an undo step)."""

    label: str
    keys: frozenset[str]
    """The keys whose state changed."""
    ignored: bool
    """What they were set to."""


def set_ignored(conn: Connection, keys: Iterable[str], ignored: bool) -> IgnoreChange:
    """Ignore keywords (they aren't listed as unmatched), or stop ignoring them."""
    wanted = {k for k in keys if k}
    current = set(
        conn.scalars(select(KeywordIgnored.match_key).where(KeywordIgnored.match_key.in_(wanted)))
    )
    changed = frozenset(wanted - current if ignored else current & wanted)
    _write_ignored(conn, changed, ignored)
    count = len(changed)
    what = "1 keyword" if count == 1 else f"{count:,} keywords"
    return IgnoreChange(f"{'Ignore' if ignored else 'Stop ignoring'} {what}", changed, ignored)


def restore_ignored(conn: Connection, change: IgnoreChange, *, forward: bool) -> None:
    """Undo (``forward=False``) or redo an :class:`IgnoreChange`."""
    _write_ignored(conn, change.keys, change.ignored if forward else not change.ignored)


def _write_ignored(conn: Connection, keys: frozenset[str], ignored: bool) -> None:
    if not keys:
        return
    if ignored:
        conn.execute(
            insert(KeywordIgnored).prefix_with("OR IGNORE"), [{"match_key": k} for k in keys]
        )
    else:
        conn.execute(delete(KeywordIgnored).where(KeywordIgnored.match_key.in_(keys)))
