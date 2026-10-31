"""The keep export (#612; DESIGN.md §4 *Backups*): everything done by hand in a keep, which
a scan of its folders can't rebuild, as one YAML file readable without Tagalot.

Items are named as the tag export names them (type, title, key, and the paths of their main
files in their watched folder), so they can be found again after a fresh scan (Import
keep…, #613). A file is named by its path in its watched folder; where two folders are
involved (linked places), by the folder's id and the path. Ids are never written: a saved
search's tags are written as paths, its item as a reference. The sections:

- ``tags``: the tag tree, as **Export tags…** writes it (pictures in a folder beside).
- ``items``: each item with anything set by hand: its tags; fields, Extra fields, a title
  edited; the keep's own fields; a note typed in Tagalot; a picture chosen; files linked
  by hand; containers, relationships, and orders set by hand; file keywords' tags and note
  fields removed.
- ``merged``, ``places``: items merged into others, and places linked as one by hand.
- ``saved searches``, ``keywords``, ``archives``, ``dismissed``: saved searches, keywords
  ignored, archives read by choice, and what Triage and Duplicates were told to hide.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, select

from tagalot.core.extra_keys import EXTRA
from tagalot.core.keep import KeepConfig
from tagalot.core.models import (
    ArchiveMemberChoice,
    ArchiveRead,
    DedupeDismissal,
    Entity,
    EntityField,
    EntityMerge,
    EntityMergeResource,
    EntityNote,
    EntityResource,
    EntityTag,
    FieldProvenance,
    FieldSource,
    FileTagRemoval,
    ItemPicture,
    KeywordIgnored,
    LinkedFolder,
    LinkedPlace,
    NoteKeyRemoval,
    PictureIgnore,
    Resource,
    ResourceStatus,
    SavedSearch,
    TriageDismissal,
    UserContains,
    UserOrder,
    UserRelation,
)
from tagalot.core.not_duplicates import EXACT, FOLDERS, SIMILAR
from tagalot.core.own_fields import OWN
from tagalot.core.own_values import raw_values
from tagalot.core.search_fields import type_labels
from tagalot.core.tag_yaml import Flow, dump_yaml, tag_path_text, tag_tree
from tagalot.core.tags import TagTree, name_key
from tagalot.core.theme_schema import ThemeSchema

FORMAT = 1
"""The export's format; an import refuses a newer one."""
HEADER = (
    "# Tagalot keep export: what was done by hand in this keep, which a scan can't rebuild.\n"
    "# Items are named by type, title, key, and files (paths in their watched folder).\n"
    "# Keep → Import keep… loads it into a keep.\n"
)
TITLE = "title"
HAND_MADE = "user"
"""``linked_place.how`` for places linked by hand (Link as one)."""
UNLINKED, UNTAGGED = "unlinked", "untagged"
"""Triage's lists whose dismissals are kept: files, and items."""
CHUNK = 500
WIDTH = 1000
"""Lines aren't wrapped: an item's files and tags stay on one line each."""


def export_keep(
    conn: Connection,
    schema: ThemeSchema,
    tree: TagTree,
    config: KeepConfig,
    pictures: Mapping[int, str] | None = None,
    icons: Mapping[int, str] | None = None,
    now: datetime | None = None,
) -> str:
    """The keep export's text; ``pictures`` and ``icons`` name the tags' picture files
    (``tag_yaml.picture_files``), written beside it by the caller."""
    return HEADER + dump_yaml(keep_data(conn, schema, tree, config, pictures, icons, now), WIDTH)


def keep_data(
    conn: Connection,
    schema: ThemeSchema,
    tree: TagTree,
    config: KeepConfig,
    pictures: Mapping[int, str] | None = None,
    icons: Mapping[int, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The keep export as data, its sections in order; empty sections are left out."""
    files = _Files(conn)
    refs = _Refs(conn, schema, files)
    data: dict[str, Any] = {
        "tagalot": "keep export",
        "format": FORMAT,
        "keep": config.name,
        "theme": config.theme.id,
        "exported": (now or datetime.now()).strftime("%Y-%m-%d %H:%M"),
        "folders": {r.id: r.name for r in config.roots},
        "tags": tag_tree(tree, schema, pictures, icons) or None,
        "items": _items(conn, schema, tree, refs, files),
        "merged": _merged(conn, refs, files),
        "places": _places(conn, files),
        "saved searches": _saved_searches(conn, tree, refs),
        "keywords": _keywords(conn),
        "archives": _archives(conn, files),
        "dismissed": _dismissed(conn, refs, files),
    }
    return {k: v for k, v in data.items() if v or k in ("tagalot", "format", "keep", "theme")}


# --- naming files and items ---


class _Files:
    """Files by id: their watched folder's id and path in it."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self.known: dict[int, tuple[str, str]] = {}

    def load(self, ids: Iterable[int]) -> None:
        wanted = sorted(set(ids) - set(self.known))
        for start in range(0, len(wanted), CHUNK):
            chunk = wanted[start : start + CHUNK]
            for rid, root, relpath in self.conn.execute(
                select(Resource.id, Resource.root_id, Resource.relpath).where(
                    Resource.id.in_(chunk)
                )
            ):
                self.known[rid] = (root, relpath)

    def path(self, rid: int) -> str | None:
        """The file's path in its watched folder."""
        self.load([rid])
        found = self.known.get(rid)
        return found[1] if found else None

    def place(self, rid: int) -> dict[str, str] | None:
        """The file's watched folder (its id) and path there."""
        self.load([rid])
        found = self.known.get(rid)
        return {"folder": found[0], "path": found[1]} if found else None


class _Refs:
    """Items named as the tag export names them: type, title, key, and main files."""

    def __init__(self, conn: Connection, schema: ThemeSchema, files: _Files) -> None:
        self.conn = conn
        self.files = files
        self.labels = type_labels(schema)
        self.primary = {
            t.type_id: next((r.name for r in t.entity.roles if r.primary), None)
            for t in schema.entities.values()
        }
        self.known: dict[int, dict[str, Any]] = {}

    def load(self, ids: Iterable[int]) -> None:
        wanted = sorted(set(ids) - set(self.known))
        for start in range(0, len(wanted), CHUNK):
            chunk = wanted[start : start + CHUNK]
            rows = {
                r.id: r
                for r in self.conn.execute(
                    select(Entity.id, Entity.type, Entity.title, Entity.ingest_key).where(
                        Entity.id.in_(chunk)
                    )
                )
            }
            main: dict[int, list[str]] = defaultdict(list)
            for entity_id, role, relpath in self.conn.execute(
                select(EntityResource.entity_id, EntityResource.role, Resource.relpath)
                .join(Resource, Resource.id == EntityResource.resource_id)
                .where(
                    EntityResource.entity_id.in_(chunk),
                    Resource.status != ResourceStatus.MISSING,
                )
                .order_by(EntityResource.sort_order, Resource.relpath)
            ):
                row = rows.get(entity_id)
                if row is not None and role is not None and role == self.primary.get(row.type):
                    main[entity_id].append(relpath)
            for entity_id, row in rows.items():
                ref: dict[str, Any] = {
                    "type": self.labels.get(row.type, row.type),
                    "title": row.title,
                }
                if row.ingest_key:
                    ref["key"] = row.ingest_key
                if main[entity_id]:
                    ref["files"] = Flow(main[entity_id])
                self.known[entity_id] = ref

    def ref(self, entity_id: int) -> dict[str, Any] | None:
        self.load([entity_id])
        found = self.known.get(entity_id)
        return dict(found) if found is not None else None


def _plain(value: Any) -> Any:
    """A stored value as YAML writes it plainly (dates as ``YYYY-MM-DD``)."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return value


# --- items ---


def _items(
    conn: Connection, schema: ThemeSchema, tree: TagTree, refs: _Refs, files: _Files
) -> list[dict[str, Any]]:
    """Every item with something set by hand, by type, then title."""
    found: dict[int, dict[str, Any]] = defaultdict(dict)

    def put(entity_id: int, key: str, value: Any) -> None:
        if value not in (None, "", [], {}):
            found[entity_id][key] = value

    tags: dict[int, list[int]] = defaultdict(list)
    for entity_id, tag_id in conn.execute(
        select(EntityTag.entity_id, EntityTag.tag_id).where(EntityTag.by_file.is_(False))
    ):
        if tag_id in tree:
            tags[entity_id].append(tag_id)
    for entity_id, ids in tags.items():
        put(entity_id, "tags", _tag_paths(tree, ids))

    edited: dict[int, set[str]] = defaultdict(set)
    sources: dict[tuple[int, str], FieldSource] = {}
    for entity_id, name, source in conn.execute(
        select(FieldProvenance.entity_id, FieldProvenance.field, FieldProvenance.source)
    ):
        sources[(entity_id, name)] = source
        if source == FieldSource.USER:
            edited[entity_id].add(name)
    for entity_id, values in _field_values(conn, schema, edited).items():
        put(entity_id, "fields", values)
    for entity_id, extra in conn.execute(select(Entity.id, Entity.extra)):
        hand = {
            name: _plain(value)
            for name, value in (extra or {}).items()
            if sources.get((entity_id, EXTRA + name), FieldSource.USER) == FieldSource.USER
        }
        put(entity_id, "extra fields", dict(sorted(hand.items(), key=lambda kv: name_key(kv[0]))))
    with_own = list(conn.scalars(select(EntityField.entity_id).distinct()))
    for start in range(0, len(with_own), CHUNK):
        for entity_id, values in raw_values(conn, with_own[start : start + CHUNK]).items():
            own = {
                name: _plain(value)
                for name, value in values.items()
                if sources.get((entity_id, OWN + name), FieldSource.USER) == FieldSource.USER
            }
            put(entity_id, "own fields", dict(sorted(own.items())))

    for entity_id, body in conn.execute(
        select(EntityNote.entity_id, EntityNote.body).where(
            EntityNote.resource_id.is_(None), EntityNote.folder_id.is_(None)
        )
    ):
        put(entity_id, "note", body)
    for entity_id, rid in conn.execute(
        select(ItemPicture.entity_id, ItemPicture.resource_id).where(
            ItemPicture.source == HAND_MADE
        )
    ):
        put(entity_id, "picture", files.path(rid))
    linked: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for entity_id, rid, role in conn.execute(
        select(EntityResource.entity_id, EntityResource.resource_id, EntityResource.role)
        .where(EntityResource.by_user.is_(True))
        .order_by(EntityResource.entity_id, EntityResource.role, EntityResource.sort_order)
    ):
        linked[entity_id].append({"role": role, "file": files.path(rid)})
    for entity_id, links in linked.items():
        put(entity_id, "files linked", links)

    _containers(conn, refs, put)
    _relations(conn, schema, refs, put)

    removed: dict[int, list[int]] = defaultdict(list)
    for entity_id, tag_id in conn.execute(select(FileTagRemoval.entity_id, FileTagRemoval.tag_id)):
        if tag_id in tree:
            removed[entity_id].append(tag_id)
    for entity_id, ids in removed.items():
        put(entity_id, "file tags removed", _tag_paths(tree, ids))
    keys: dict[int, list[str]] = defaultdict(list)
    for entity_id, key in conn.execute(
        select(NoteKeyRemoval.entity_id, NoteKeyRemoval.key).order_by(NoteKeyRemoval.key)
    ):
        keys[entity_id].append(key)
    for entity_id, names in keys.items():
        put(entity_id, "note fields removed", Flow(names))

    refs.load(found)
    written = []
    for entity_id, sections in found.items():
        ref = refs.ref(entity_id)
        if ref is not None:
            written.append({**ref, **sections})
    written.sort(key=lambda i: (name_key(i["type"]), name_key(i["title"])))
    return written


def _tag_paths(tree: TagTree, ids: Iterable[int]) -> list[Any]:
    paths = [tag_path_text(tree, t) for t in ids]
    paths.sort(key=lambda p: [name_key(n) for n in (p if isinstance(p, list) else [p])])
    texts = [p for p in paths if isinstance(p, str)]
    return Flow(texts) if len(texts) == len(paths) else paths


def _field_values(
    conn: Connection, schema: ThemeSchema, edited: Mapping[int, set[str]]
) -> dict[int, dict[str, Any]]:
    """Each item's edited title and theme fields, by field name."""
    found: dict[int, dict[str, Any]] = defaultdict(dict)
    ids = sorted(edited)
    by_type: dict[str, list[int]] = defaultdict(list)
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start : start + CHUNK]
        for entity_id, type_id, title in conn.execute(
            select(Entity.id, Entity.type, Entity.title).where(Entity.id.in_(chunk))
        ):
            if TITLE in edited[entity_id]:
                found[entity_id][TITLE] = title
            by_type[type_id].append(entity_id)
    for type_id, of_type in by_type.items():
        try:
            table = schema.by_type_id(type_id)
        except KeyError:
            continue  # a type the theme no longer has
        names = [f.name for f in table.fields]
        for start in range(0, len(of_type), CHUNK):
            chunk = of_type[start : start + CHUNK]
            for row in conn.execute(select(table.table).where(table.table.c.id.in_(chunk))):
                values = row._mapping
                for name in names:
                    if name in edited[values["id"]]:
                        found[values["id"]][name] = _plain(values[name])
    return found


def _containers(conn: Connection, refs: _Refs, put: Any) -> None:
    """On each item, the containers it was put in or taken out of by hand."""
    put_in: dict[int, list[int]] = defaultdict(list)
    taken: dict[int, list[int]] = defaultdict(list)
    for parent, child, added in conn.execute(
        select(UserContains.parent_id, UserContains.child_id, UserContains.added)
    ):
        (put_in if added else taken)[child].append(parent)
    refs.load([p for ps in (*put_in.values(), *taken.values()) for p in ps])
    for child, parents in put_in.items():
        put(child, "put in", [r for p in parents if (r := refs.ref(p))])
    for child, parents in taken.items():
        put(child, "taken out of", [r for p in parents if (r := refs.ref(p))])


def _relations(conn: Connection, schema: ThemeSchema, refs: _Refs, put: Any) -> None:
    """On each ``a`` side, the relationships made or removed by hand; on each ``b`` side of
    an ordered one put in order by hand, its items in that order."""
    made: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    removed: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for name, a_id, b_id, added in conn.execute(
        select(UserRelation.name, UserRelation.a_id, UserRelation.b_id, UserRelation.added)
    ):
        (made if added else removed)[a_id][name].append(b_id)
    orders: dict[int, dict[str, list[int]]] = defaultdict(dict)
    for name, b_id in conn.execute(select(UserOrder.name, UserOrder.b_id)):
        rel = schema.relationships.get(name)
        if rel is None:
            continue
        table = rel.table
        orders[b_id][name] = list(
            conn.scalars(
                select(table.c.a_id)
                .where(table.c.b_id == b_id)
                .order_by(table.c.position, table.c.a_id)
            )
        )
    refs.load(
        [
            i
            for d in (*made.values(), *removed.values(), *orders.values())
            for v in d.values()
            for i in v
        ]
    )

    def named(lists: Mapping[str, list[int]]) -> dict[str, list[dict[str, Any]]]:
        return {name: [r for i in ids if (r := refs.ref(i))] for name, ids in sorted(lists.items())}

    for a_id, lists in made.items():
        put(a_id, "related", named(lists))
    for a_id, lists in removed.items():
        put(a_id, "unrelated", named(lists))
    for b_id, lists in orders.items():
        put(b_id, "ordered", named(lists))


# --- the rest ---


def _merged(conn: Connection, refs: _Refs, files: _Files) -> list[dict[str, Any]]:
    """Items merged into others: what the merged item was, and the item it is now part of."""
    merged_files: dict[int, list[int]] = defaultdict(list)
    for merged_id, rid in conn.execute(
        select(EntityMergeResource.merged_id, EntityMergeResource.resource_id)
    ):
        merged_files[merged_id].append(rid)
    rows = conn.execute(
        select(
            EntityMerge.merged_id, EntityMerge.type, EntityMerge.ingest_key, EntityMerge.into_id
        ).order_by(EntityMerge.merged_at, EntityMerge.merged_id)
    ).all()
    refs.load([_last(r.into_id, rows) for r in rows])
    written = []
    for row in rows:
        into = refs.ref(_last(row.into_id, rows))  # where it is now, after later merges
        if into is None:
            continue
        item: dict[str, Any] = {"type": refs.labels.get(row.type, row.type)}
        if row.ingest_key:
            item["key"] = row.ingest_key
        paths = sorted(p for rid in merged_files[row.merged_id] if (p := files.path(rid)))
        if paths:
            item["files"] = Flow(paths)
        written.append({"item": item, "into": into})
    return written


def _last(into: int, rows: Iterable[Any]) -> int:
    """Where a chain of merges ends."""
    onward = {r.merged_id: r.into_id for r in rows}
    seen = set()
    while into in onward and into not in seen:
        seen.add(into)
        into = onward[into]
    return into


def _places(conn: Connection, files: _Files) -> list[dict[str, Any]]:
    """Places linked as one by hand, and folders linked as one."""
    written: list[dict[str, Any]] = []
    for rid, first in conn.execute(
        select(LinkedPlace.resource_id, LinkedPlace.first_id)
        .where(LinkedPlace.how == HAND_MADE)
        .order_by(LinkedPlace.linked_at)
    ):
        place, same = files.place(rid), files.place(first)
        if place and same:
            written.append({"file": place, "same as": same})
    for root, relpath, first_root, first_relpath in conn.execute(
        select(
            LinkedFolder.root_id,
            LinkedFolder.relpath,
            LinkedFolder.first_root_id,
            LinkedFolder.first_relpath,
        ).order_by(LinkedFolder.linked_at)
    ):
        written.append(
            {
                "folder": {"folder": root, "path": relpath},
                "same as": {"folder": first_root, "path": first_relpath},
            }
        )
    return written


def _saved_searches(conn: Connection, tree: TagTree, refs: _Refs) -> list[dict[str, Any]]:
    """Saved searches, their tags by path and their item named, not by id."""
    written = []
    for name, definition in conn.execute(
        select(SavedSearch.name, SavedSearch.definition).order_by(SavedSearch.id)
    ):
        written.append({"name": name, "search": _named_search(definition, tree, refs)})
    return written


def _named_search(data: Any, tree: TagTree, refs: _Refs) -> Any:
    """A stored search with its tag ids as paths and item ids as references."""
    if not isinstance(data, Mapping):
        return data
    named: dict[str, Any] = {}
    for key, value in data.items():
        if key in ("include", "exclude") and isinstance(value, list):
            named[key] = [tag_path_text(tree, t) for t in value if isinstance(t, int) and t in tree]
        elif key == "within" and isinstance(value, int):
            named[key] = refs.ref(value)
        elif key == "related" and isinstance(value, list) and len(value) == 2:
            named[key] = [value[0], refs.ref(value[1]) if isinstance(value[1], int) else None]
        elif isinstance(value, Mapping):
            named[key] = _named_search(value, tree, refs)
        else:
            named[key] = value
    return named


def _keywords(conn: Connection) -> dict[str, Any]:
    ignored = sorted(conn.scalars(select(KeywordIgnored.match_key)))
    return {"ignored": Flow(ignored)} if ignored else {}


def _archives(conn: Connection, files: _Files) -> list[dict[str, Any]]:
    """Archives whose inside was chosen for (all, none, or member by member)."""
    choices: dict[int, dict[str, bool]] = defaultdict(dict)
    for archive, path, read in conn.execute(
        select(
            ArchiveMemberChoice.archive_id, ArchiveMemberChoice.path, ArchiveMemberChoice.read
        ).where(ArchiveMemberChoice.read.is_not(None))
    ):
        choices[archive][path] = bool(read)
    written = []
    for rid, mode in conn.execute(
        select(ArchiveRead.resource_id, ArchiveRead.mode).where(ArchiveRead.mode.is_not(None))
    ):
        place = files.place(rid)
        if place is None or mode is None:
            continue
        entry: dict[str, Any] = {**place, "read": str(mode)}
        if choices[rid]:
            entry["members"] = dict(sorted(choices[rid].items()))
        written.append(entry)
    written.sort(key=lambda e: (e["folder"], e["path"]))
    return written


def _dismissed(conn: Connection, refs: _Refs, files: _Files) -> dict[str, Any]:
    """What Triage and Duplicates were told to hide, and Triage's picture rules."""
    unlinked, untagged = [], []
    for name, target in conn.execute(
        select(TriageDismissal.list, TriageDismissal.target_id).order_by(
            TriageDismissal.list, TriageDismissal.target_id
        )
    ):
        if name == UNLINKED and (place := files.place(target)):
            unlinked.append(place)
        elif name == UNTAGGED and (ref := refs.ref(target)):
            untagged.append(ref)
    identical, similar, folders = [], [], []
    for kind, key in conn.execute(
        select(DedupeDismissal.kind, DedupeDismissal.key).order_by(DedupeDismissal.dismissed_at)
    ):
        if kind == EXACT:
            identical.append(key)  # the files' fingerprint: the same wherever they are
        elif kind == SIMILAR:
            pair = [refs.ref(int(i)) for i in key.split(":") if i.isdigit()]
            if len(pair) == 2 and all(pair):
                similar.append(pair)
        elif kind == FOLDERS:
            sides = [line.partition(":") for line in key.splitlines()]
            if len(sides) == 2:
                folders.append([{"folder": root, "path": path} for root, _, path in sides])
    pictures: list[dict[str, Any]] = []
    for scope, entity_id, value, type_id in conn.execute(
        select(
            PictureIgnore.scope, PictureIgnore.entity_id, PictureIgnore.value, PictureIgnore.type
        ).order_by(PictureIgnore.id)
    ):
        if scope == "item" and entity_id is not None and (ref := refs.ref(entity_id)):
            pictures.append({"item": ref})
        elif scope == "value" and value:
            pictures.append({"value": value})
        elif scope == "type" and type_id:
            pictures.append({"type": refs.labels.get(type_id, type_id)})
    data = {
        "unlinked files": unlinked,
        "untagged items": untagged,
        "identical files": Flow(identical) if identical else [],
        "similar items": similar,
        "folder pairs": folders,
        "picture problems": pictures,
    }
    return {k: v for k, v in data.items() if v}
