"""Links in a note to other items (DESIGN.md §9 *Notes*, *Links to items*, #671).

A note can name another item two ways, as editors write them:

- an Obsidian wiki link, ``[[Alice]]`` or ``[[People/Alice|her]]``: the item whose note
  file is called that (``Alice.md``; with a folder part, a note file at that path's end),
  else the items titled that;
- a relative Markdown link to a note file in the keep, ``[Alice](../People/Alice.md)``:
  that note's item.

The page's worker finds them (:func:`find_wiki_items`, :func:`find_note_file_items`), so
following one is a lookup in what was read. A link that names no item stays what it was:
a wiki link its text, a file link the file. Nothing here writes anything.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote, unquote

from sqlalchemy import Connection, func, or_, select

from tagalot.core.models import Entity, EntityNote, Resource
from tagalot.core.note_markdown import MAX_LINKS, find_file, link_refs
from tagalot.core.roots import place_in_roots

ITEM_SCHEME = "tagalot-item"
"""The scheme of a wiki link that names items, as rendered: ``tagalot-item:Alice``."""
NOTE_EXTENSIONS = (".md", ".markdown")
"""A link to a file with one of these may be a link to a note."""

_WIKI_LINK = re.compile(r"(?<!!)\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
"""Obsidian's ``[[Other note]]`` and ``[[Other note|shown as]]`` (as ``note_markdown``)."""


@dataclass(frozen=True)
class LinkedItem:
    """An item a note's link names: its id, title, and type's id."""

    id: int
    title: str
    type: str


def wiki_key(name: str) -> str:
    """How a wiki link's target is compared: case ignored, slashes forward, and without a
    note extension (``People/alice.md`` and ``people/Alice`` are one)."""
    key = name.strip().replace("\\", "/").casefold()
    for ext in NOTE_EXTENSIONS:
        if key.endswith(ext):
            return key[: -len(ext)]
    return key


def wiki_names(body: str) -> list[str]:
    """The targets of ``body``'s wiki links (not ``![[embeds]]``), as written, in order,
    each once (by :func:`wiki_key`), at most :data:`MAX_LINKS`."""
    found: dict[str, str] = {}
    for match in _WIKI_LINK.finditer(body):
        name = match.group(1).strip()
        if name:
            found.setdefault(wiki_key(name), name)
    return list(found.values())[:MAX_LINKS]


def item_url(name: str) -> str:
    """The link a wiki link that names items is rendered with."""
    return f"{ITEM_SCHEME}:{quote(wiki_key(name), safe='')}"


def url_key(url: str) -> str | None:
    """The :func:`wiki_key` an :func:`item_url` stands for, or ``None`` for another link."""
    prefix = f"{ITEM_SCHEME}:"
    return unquote(url[len(prefix) :]) if url.startswith(prefix) else None


def link_wiki_items(body: str, keys: Iterable[str]) -> str:
    """``body`` with the wiki links whose :func:`wiki_key` is in ``keys`` written as
    Markdown links to :func:`item_url`, so they can be followed; the rest are left for
    ``note_markdown.renderable`` (which shows them as text)."""
    wanted = set(keys)
    if not wanted:
        return body

    def link(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if wiki_key(name) not in wanted:
            return match.group(0)
        shown = (match.group(2) or name).strip().replace("[", "\\[").replace("]", "\\]")
        return f"[{shown}]({item_url(name)})"

    return _WIKI_LINK.sub(link, body)


def _written_stem(name: str) -> str:
    """A wiki link target's note name as written, without its folder or extension."""
    stem = name.strip().replace("\\", "/").rpartition("/")[2]
    for ext in NOTE_EXTENSIONS:
        if stem.casefold().endswith(ext):
            return stem[: -len(ext)]
    return stem


def find_wiki_items(conn: Connection, names: Sequence[str]) -> dict[str, list[LinkedItem]]:
    """The items each wiki link target in ``names`` names, by :func:`wiki_key`: those whose
    note file has that name (and, for ``Folder/Name``, ends with that path), else those
    titled that. Targets that name nothing are left out."""
    keys = {wiki_key(n): n for n in names if n.strip()}
    if not keys:
        return {}
    found: dict[str, list[LinkedItem]] = {}
    stems = {case for n in keys.values() for case in _cases(_written_stem(n))}
    note_rows = conn.execute(
        select(Entity.id, Entity.title, Entity.type, Resource.relpath)
        .join(EntityNote, EntityNote.entity_id == Entity.id)
        .join(Resource, Resource.id == EntityNote.resource_id)
        .where(
            or_(
                *(
                    Resource.relpath.like(f"%{_like(stem)}{ext}", escape="\\")
                    for stem in stems
                    for ext in NOTE_EXTENSIONS
                )
            )
        )
        .order_by(Entity.title, Entity.id)
    ).all()
    for key in keys:
        for entity_id, title, type_, relpath in note_rows:
            if _note_path_matches(key, relpath):
                _add(found, key, LinkedItem(entity_id, title, type_))
    untitled = {k: n for k, n in keys.items() if k not in found}
    if untitled:
        written = {case for n in untitled.values() for case in _cases(n.strip())}
        lowered = {n.lower() for n in written}
        rows = conn.execute(
            select(Entity.id, Entity.title, Entity.type)
            .where(or_(Entity.title.in_(written), func.lower(Entity.title).in_(lowered)))
            .order_by(Entity.title, Entity.id)
        ).all()
        for key, name in untitled.items():
            for entity_id, title, type_ in rows:
                if title.casefold() == name.strip().casefold():
                    _add(found, key, LinkedItem(entity_id, title, type_))
    return found


def _cases(text: str) -> set[str]:
    """``text`` as written and in the usual cases. SQLite ignores only ASCII case, so a
    link to ``émile`` finds ``Émile`` this way."""
    return {text, text.lower(), text.upper(), text.capitalize(), text.title()}


def _like(text: str) -> str:
    """``text`` for a LIKE pattern (which ignores ASCII case): its wildcards taken as
    themselves."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _note_path_matches(key: str, relpath: str) -> bool:
    """Whether the note file at ``relpath`` is the one wiki target ``key`` names: its name
    is the target's, and a target with a folder part ends the path."""
    stem = wiki_key(relpath)
    if not relpath.casefold().endswith(NOTE_EXTENSIONS):
        return False
    return stem == key or stem.endswith(f"/{key}")


def _add(found: dict[str, list[LinkedItem]], key: str, item: LinkedItem) -> None:
    items = found.setdefault(key, [])
    if item not in items:
        items.append(item)


def find_note_file_items(
    conn: Connection,
    body: str,
    folder: str,
    assets: Sequence[str],
    roots: Mapping[str, str | None],
) -> dict[str, list[LinkedItem]]:
    """The items of the note files ``body``'s relative links name: ``{path on this computer,
    as the link is followed: items}``. ``folder`` is the note's folder, ``assets`` its
    assets folders, ``roots`` root id -> this computer's path. Checks the disk (for links in
    an assets folder): call it from a worker."""
    places: dict[tuple[str, str], str] = {}
    for ref in link_refs(body)[:MAX_LINKS]:
        clean = unquote(ref.split("#", 1)[0].split("?", 1)[0])
        if not clean.casefold().endswith(NOTE_EXTENSIONS):
            continue
        path = find_file(folder, ref, assets)
        place = place_in_roots(path, roots) if path is not None else None
        if path is not None and place is not None:
            places[place] = path
    if not places:
        return {}
    found: dict[str, list[LinkedItem]] = {}
    for root_id in {root for root, _ in places}:
        relpaths = [rel for root, rel in places if root == root_id]
        rows = conn.execute(
            select(Entity.id, Entity.title, Entity.type, Resource.relpath)
            .join(EntityNote, EntityNote.entity_id == Entity.id)
            .join(Resource, Resource.id == EntityNote.resource_id)
            .where(Resource.root_id == root_id, Resource.relpath.in_(relpaths))
            .order_by(Entity.title, Entity.id)
        ).all()
        for entity_id, title, type_, relpath in rows:
            _add(found, places[(root_id, relpath)], LinkedItem(entity_id, title, type_))
    _find_ignoring_case(conn, places, found)  # a link written in another case (#671)
    return found


def _find_ignoring_case(
    conn: Connection,
    places: Mapping[tuple[str, str], str],
    found: dict[str, list[LinkedItem]],
) -> None:
    """Links whose path matched no note exactly: the note at that path ignoring case, as
    a Mac's or Windows' file system finds it."""
    missing = {
        (root, rel.casefold()): (rel, path)
        for (root, rel), path in places.items()
        if path not in found
    }
    if not missing:
        return
    rows = conn.execute(
        select(Entity.id, Entity.title, Entity.type, Resource.root_id, Resource.relpath)
        .join(EntityNote, EntityNote.entity_id == Entity.id)
        .join(Resource, Resource.id == EntityNote.resource_id)
        .where(
            or_(*(Resource.relpath.like(_like(rel), escape="\\") for rel, _ in missing.values()))
        )
        .order_by(Entity.title, Entity.id)
    ).all()
    for entity_id, title, type_, root_id, relpath in rows:
        hit = missing.get((root_id, relpath.casefold()))
        if hit is not None:
            _add(found, hit[1], LinkedItem(entity_id, title, type_))
