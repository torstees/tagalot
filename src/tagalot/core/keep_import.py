"""Import keep… (#613; DESIGN.md §4 *Backups*): a keep export (#612) loaded into a keep.

Items are found as Import tags… finds them (:func:`~tagalot.core.tag_yaml.find_item`): by
key, else by their files' paths, else by type and title when exactly one item has it.
Anything not found is listed, never guessed. Files are found by their path in their watched
folder: the folder with the export's folder id, else the one with its name.

What the file says is done as the keep's own commands do it (tagging, editing a field,
linking a file, putting in a container, merging, linking places…), in this order: merges,
places, the tag tree and items' tags, the rest of each item, then the keep's records (saved
searches, ignored keywords, archives, dismissals). A value the keep already has set by hand
that differs from the file's is a conflict: left as it is unless ``replace``. Whatever is
there already is left alone, so importing the same file twice changes nothing.

:func:`import_keep` does it on a connection and returns what it did, with the undo steps
of each part; :class:`~tagalot.core.tag_service.TagService` runs it as one undo step, and
previews it by running it in a transaction it rolls back.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Connection, delete, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot.core.actions import ActionChange
from tagalot.core.containers import (
    ContainerError,
    contain_by_hand,
    record_contains,
    uncontain_by_hand,
)
from tagalot.core.entity_state import ChangeRecorder
from tagalot.core.extra_keys import EXTRA
from tagalot.core.fields import (
    FieldEditError,
    edit_extra,
    edit_field,
    edit_note,
    edit_own_field,
)
from tagalot.core.item_pictures import PictureChange, PictureError, set_picture
from tagalot.core.keep_export import FORMAT, HAND_MADE, TITLE, UNLINKED, UNTAGGED, plain_value
from tagalot.core.keywords import forget_removals, set_ignored
from tagalot.core.linked_folders import folder_of, link_folders
from tagalot.core.links import LinkError, link_files
from tagalot.core.merge import MergeError, merge_items
from tagalot.core.models import (
    ArchiveMemberChoice,
    ArchiveMode,
    ArchiveRead,
    DedupeDismissal,
    Entity,
    EntityContains,
    EntityNote,
    EntityResource,
    FieldProvenance,
    FieldSource,
    FileTagRemoval,
    ItemPicture,
    LinkedFolder,
    NoteKeyRemoval,
    Resource,
    Root,
    SavedSearch,
    TriageDismissal,
    UserContains,
    UserOrder,
    UserRelation,
    utcnow,
)
from tagalot.core.not_duplicates import EXACT, FOLDERS, SIMILAR, set_not_duplicate
from tagalot.core.own_fields import OwnFields
from tagalot.core.own_values import raw_values
from tagalot.core.picture_problems import (
    ITEM,
    TYPE,
    VALUE,
    PictureIgnoreChange,
    PictureRule,
    picture_problems,
    picture_rules,
    restore_picture_ignores,
    rule_for,
)
from tagalot.core.place_links import PlaceError, link_place
from tagalot.core.places import first_place
from tagalot.core.relations import (
    RelationError,
    add_related,
    record_relation,
    remove_related,
)
from tagalot.core.saved_searches import (
    SavedDefinition,
    SavedSearchError,
    find_by_name,
    save_search,
)
from tagalot.core.search_fields import type_labels
from tagalot.core.tag_yaml import ImportPlan, Item, dump_yaml, find_item, import_tags, resolve_path
from tagalot.core.tags import (
    TagError,
    TagTree,
    clean_name,
    search_key,
    split_tag_path,
    tag_entities,
)
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.triage import dismiss

KEEP_EXPORT = "keep export"
"""What a keep export's ``tagalot:`` says."""


class KeepImportError(ValueError):
    """The file isn't a keep export this keep can load; the message is for the user."""


@dataclass(frozen=True)
class KeepImportPlan:
    """What importing a keep export did (or, previewed, would do)."""

    tags: ImportPlan = field(default_factory=ImportPlan)
    """The tag tree's part, as Import tags… reports it."""
    found: int = 0
    """Items the file lists that were found in this keep."""
    not_found: tuple[str, ...] = ()
    """Items it names that weren't, described ("Paper “Small Gods”")."""
    changed: tuple[str, ...] = ()
    """Each item changed, and how ("Paper “X”: tags, Year, note")."""
    records: tuple[str, ...] = ()
    """Merges, places, saved searches, keywords, archives, and dismissals, one a line."""
    conflicts: tuple[str, ...] = ()
    """Values set by hand here that differ from the file's."""
    problems: tuple[str, ...] = ()
    """What the file says that can't be done here: left out."""
    replace: bool = False
    """Whether the conflicts were settled with the file's values."""

    @property
    def changes(self) -> bool:
        return bool(self.tags.changes or self.changed or self.records)


@dataclass(frozen=True)
class ArchiveChange:
    """Archives' reading choices before and after an import; an undo step's part."""

    label: str
    ids: tuple[int, ...]
    before: tuple[tuple[tuple[int, str | None], ...], tuple[tuple[int, str, bool | None], ...]]
    after: tuple[tuple[tuple[int, str | None], ...], tuple[tuple[int, str, bool | None], ...]]


TagPhase = Callable[[Connection, Callable[[Connection], ImportPlan]], tuple[ImportPlan, Any]]
"""Runs the tag part, recording it as Tag manager's steps are recorded: ``(plan, step)``."""


def read_export(text: str, theme_id: str) -> dict[str, Any]:
    """The keep export in ``text``, checked: a keep export, a format this version reads, of
    a keep with the theme ``theme_id``. Raises :class:`KeepImportError`."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise KeepImportError(f"it isn't readable YAML ({e.__class__.__name__})") from None
    if not isinstance(data, dict) or data.get("tagalot") != KEEP_EXPORT:
        raise KeepImportError(
            "it isn't a keep export (Keep → Export keep… makes one; Import tags… loads a tag file)"
        )
    version = data.get("format")
    if not isinstance(version, int) or version > FORMAT:
        raise KeepImportError("it was made by a newer version of Tagalot")
    theme = data.get("theme")
    if theme != theme_id:
        raise KeepImportError(
            f"it is of a keep with the {theme!r} theme, and this keep has {theme_id!r}"
        )
    return data


def import_keep(
    conn: Connection,
    schema: ThemeSchema,
    data: Mapping[str, Any],
    *,
    own_fields: OwnFields,
    tag_phase: TagPhase,
    base: Path | None = None,
    replace: bool = False,
) -> tuple[KeepImportPlan, list[Any]]:
    """Do what the keep export ``data`` (from :func:`read_export`) says, on ``conn``.
    ``base`` is the file's folder (the tags' pictures are there). Returns what was done and
    its undo steps, in the order done."""
    run = _Import(conn, schema, data, own_fields, replace)
    run.merges(_list(data.get("merged")))
    run.places(_list(data.get("places")))
    tags_text = dump_yaml(data["tags"]) if isinstance(data.get("tags"), dict) else ""
    items = [i for i in _list(data.get("items")) if isinstance(i, dict)]

    def tag_part(conn: Connection) -> ImportPlan:
        plan = import_tags(conn, tags_text, schema, base=base) if tags_text else ImportPlan()
        run.item_tags(items)
        return plan

    tag_plan, tag_step = tag_phase(conn, tag_part)
    run.steps.append(tag_step)
    run.items()
    run.saved_searches(_list(data.get("saved searches")))
    run.keywords(data.get("keywords"))
    run.archives(_list(data.get("archives")))
    run.dismissed(data.get("dismissed"))
    plan = KeepImportPlan(
        tags=tag_plan,
        found=len(run.found_items),
        not_found=tuple(dict.fromkeys(run.not_found)),
        changed=tuple(f"{run.describe(i)}: {', '.join(w)}" for i, w in run.changed.items()),
        records=tuple(run.records),
        conflicts=tuple(run.conflicts),
        problems=tuple((*(str(p) for p in tag_plan.problems), *run.problems)),
        replace=replace,
    )
    return plan, run.steps


def restore_archives(conn: Connection, change: ArchiveChange, *, forward: bool) -> None:
    """Undo (``forward=False``) or redo an :class:`ArchiveChange`."""
    reads, members = change.after if forward else change.before
    ids = list(change.ids)
    conn.execute(delete(ArchiveMemberChoice).where(ArchiveMemberChoice.archive_id.in_(ids)))
    conn.execute(update(ArchiveRead).where(ArchiveRead.resource_id.in_(ids)).values(mode=None))
    for rid, mode in reads:
        _set_mode(conn, rid, mode)
    if members:
        conn.execute(
            insert(ArchiveMemberChoice),
            [{"archive_id": a, "path": p, "read": r} for a, p, r in members],
        )


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _tag_names(value: Any) -> tuple[str, ...] | None:
    """A tag path as the export writes it: ``A > B``, or a list of names."""
    try:
        if isinstance(value, list):
            return tuple(clean_name(str(n)) for n in value)
        return tuple(split_tag_path(str(value)))
    except TagError:
        return None


def _shown(value: Any) -> str:
    text = str(plain_value(value)).replace("\n", " ")
    return repr(text if len(text) <= 40 else text[:39] + "…")


class _Import:
    """One import's state: what was found, done, and left out."""

    def __init__(
        self,
        conn: Connection,
        schema: ThemeSchema,
        data: Mapping[str, Any],
        own_fields: OwnFields,
        replace: bool,
    ) -> None:
        self.conn = conn
        self.schema = schema
        self.own_fields = own_fields
        self.replace = replace
        self.labels = dict(type_labels(schema))
        self.kinds = {search_key(label): t for t, label in self.labels.items()}
        self.kinds.update({search_key(t): t for t in self.labels})
        roots = {r: n for r, n in conn.execute(select(Root.id, Root.name))}
        named = {search_key(n): r for r, n in roots.items()}
        folders = data.get("folders")
        self.roots: dict[str, str | None] = {}
        for folder_id, name in folders.items() if isinstance(folders, dict) else ():
            folder = str(folder_id)
            self.roots[folder] = folder if folder in roots else named.get(search_key(str(name)))
        self.steps: list[Any] = []
        self.found_items: set[int] = set()
        self.not_found: list[str] = []
        self.changed: dict[int, list[str]] = {}
        self.records: list[str] = []
        self.conflicts: list[str] = []
        self.problems: list[str] = []
        self.cache: dict[tuple[Any, ...], int | None] = {}
        self.matched: list[tuple[int, dict[str, Any]]] = []
        self.recorder: ChangeRecorder | None = None

    # --- finding ---

    def find(self, ref: Any, *, listed: bool = True) -> int | None:
        """The item an item reference names; one not found is listed (``listed``)."""
        if not isinstance(ref, dict):
            return None
        type_id = self.kinds.get(search_key(str(ref.get("type") or "")))
        title = str(ref.get("title") or "").strip()
        files = tuple(str(f) for f in _list(ref.get("files")))
        key = str(ref["key"]) if ref.get("key") else None
        described = f"{ref.get('type')} “{title or key or (files[:1] or ('?',))[0]}”"
        if type_id is None:
            if listed:
                self.problems.append(
                    f"{described}: {ref.get('type')!r} isn't a kind of item in this keep."
                )
            return None
        cache_key = (type_id, title, key, files)
        if cache_key not in self.cache:
            self.cache[cache_key] = find_item(self.conn, Item(None, type_id, title, key, files, ()))
        found = self.cache[cache_key]
        if found is None and listed:
            self.not_found.append(described)
        return found

    def resource(self, place: Any) -> int | None:
        """A file named by its folder's id and path (``{folder, path}``) or by its path."""
        if isinstance(place, dict):
            root = self.roots.get(str(place.get("folder")), str(place.get("folder")))
            path = str(place.get("path") or "")
            if root is None:
                self.problems.append(f"{path}: its watched folder isn't in this keep.")
                return None
            found = self.conn.scalar(
                select(Resource.id).where(Resource.root_id == root, Resource.relpath == path)
            )
            if found is None:
                self.problems.append(f"{path}: not found in this keep.")
            return found
        path = str(place or "")
        ids = list(self.conn.scalars(select(Resource.id).where(Resource.relpath == path)))
        if len(ids) != 1:
            self.problems.append(
                f"{path}: {'not found' if not ids else 'in more than one watched folder'} "
                "in this keep."
            )
            return None
        return int(ids[0])

    def folder(self, place: Any) -> tuple[str, str] | None:
        if not isinstance(place, dict):
            return None
        root = self.roots.get(str(place.get("folder")), str(place.get("folder")))
        path = str(place.get("path") or "")
        if root is None:
            self.problems.append(f"{path}: its watched folder isn't in this keep.")
            return None
        return root, path

    def describe(self, entity_id: int) -> str:
        row = self.conn.execute(
            select(Entity.type, Entity.title).where(Entity.id == entity_id)
        ).first()
        if row is None:
            return f"Item {entity_id}"
        return f"{self.labels.get(row.type, row.type)} “{row.title}”"

    def did(self, entity_id: int, what: str) -> None:
        done = self.changed.setdefault(entity_id, [])
        if what not in done:
            done.append(what)

    def conflict(self, entity_id: int, what: str, here: Any, there: Any) -> bool:
        """A value set by hand here that differs: listed; whether to replace it."""
        self.conflicts.append(
            f"{self.describe(entity_id)}: {what} is {_shown(here)} here, "
            f"{_shown(there)} in the file"
        )
        return self.replace

    # --- merges and places ---

    def merges(self, merged: list[Any]) -> None:
        for entry in merged:
            if not isinstance(entry, dict):
                continue
            into = self.find(entry.get("into"))
            item = entry.get("item")
            if into is None or not isinstance(item, dict):
                continue
            ref = {**item, "title": ""}
            other = self.find(ref, listed=False) if item.get("key") or item.get("files") else None
            if other is None or other == into:
                continue  # merged here already, or not scanned again
            try:
                change = merge_items(self.conn, self.schema, into, [other])
            except MergeError as e:
                self.problems.append(f"Merging into {self.describe(into)}: {e}")
                continue
            self.steps.append(change)
            self.records.append(f"Merged a {item.get('type')} into {self.describe(into)}")
            self.cache.clear()

    def places(self, places: list[Any]) -> None:
        for entry in places:
            if not isinstance(entry, dict):
                continue
            try:
                if "folder" in entry:
                    self.linked_folders(entry)
                else:
                    self.linked_place(entry)
            except PlaceError as e:
                self.problems.append(str(e))
        self.cache.clear()

    def linked_place(self, entry: dict[str, Any]) -> None:
        place, first = self.resource(entry.get("file")), self.resource(entry.get("same as"))
        if place is None or first is None:
            return
        if first_place(self.conn, place) == first_place(self.conn, first):
            return
        self.steps.append(link_place(self.conn, self.schema, place, first, HAND_MADE))
        self.records.append(f"Linked {entry['file'].get('path')} as one with its copy")

    def linked_folders(self, entry: dict[str, Any]) -> None:
        other, first = self.folder(entry.get("folder")), self.folder(entry.get("same as"))
        if other is None or first is None:
            return
        if self.conn.execute(
            select(LinkedFolder.root_id).where(
                LinkedFolder.root_id == other[0], LinkedFolder.relpath == other[1]
            )
        ).first():
            return
        change = link_folders(
            self.conn, self.schema, folder_of(self.conn, *first), folder_of(self.conn, *other)
        )
        self.steps.append(change)
        self.records.append(f"Linked the folder {other[1]} as one with {first[1]}")

    # --- items: tags (in the tag step) ---

    def item_tags(self, items: list[dict[str, Any]]) -> None:
        """Find the items, and put on their tags and the file tags taken off them."""
        tree = TagTree.load(self.conn)
        self.matched = []
        for entry in items:
            entity_id = self.find(entry)
            if entity_id is None:
                continue
            self.found_items.add(entity_id)
            self.matched.append((entity_id, entry))
            wanted = self.tag_ids(tree, entry, "tags")
            if wanted:
                added = tag_entities(self.conn, [entity_id], wanted)
                if added:
                    forget_removals(self.conn, {(e, t) for e, t, _ in added})
                    self.did(entity_id, "tags")
            removed = self.tag_ids(tree, entry, "file tags removed")
            have = set(
                self.conn.scalars(
                    select(FileTagRemoval.tag_id).where(FileTagRemoval.entity_id == entity_id)
                )
            )
            new = [t for t in removed if t not in have]
            if new:
                self.conn.execute(
                    insert(FileTagRemoval), [{"entity_id": entity_id, "tag_id": t} for t in new]
                )
                self.did(entity_id, "file tags removed")

    def tag_ids(self, tree: TagTree, entry: dict[str, Any], key: str) -> list[int]:
        found = []
        raw = entry.get(key) or []
        for value in raw if isinstance(raw, list) else [raw]:
            names = _tag_names(value)
            tag_id = resolve_path(tree, names) if names else None
            if tag_id is None:
                self.problems.append(f"{entry.get('title')!r}: no tag {value!r} in this keep.")
            else:
                found.append(tag_id)
        return found

    # --- items: the rest (one recorder) ---

    def touch(self, *ids: int) -> None:
        assert self.recorder is not None
        self.recorder.touch(ids)

    def items(self) -> None:
        self.recorder = ChangeRecorder(self.conn, self.schema)
        for entity_id, entry in self.matched:
            self.fields(entity_id, entry.get("fields"))
            self.extra(entity_id, entry.get("extra fields"))
            self.own(entity_id, entry.get("own fields"))
            self.note(entity_id, entry.get("note"))
            self.note_removals(entity_id, entry.get("note fields removed"))
            self.picture(entity_id, entry.get("picture"))
            self.links(entity_id, entry.get("files linked"))
            self.containers(entity_id, entry)
            self.relations(entity_id, entry)
        for entity_id, entry in self.matched:  # after every item's relationships
            self.orders(entity_id, entry.get("ordered"))
        before = self.recorder.before
        if before:
            self.steps.append(ActionChange("Import items", before, self.recorder.after()))

    def fields(self, entity_id: int, fields: Any) -> None:
        if not isinstance(fields, dict):
            return
        type_id = self.conn.scalar(select(Entity.type).where(Entity.id == entity_id))
        try:
            table = self.schema.by_type_id(str(type_id))
        except KeyError:
            return
        infos = {f.name: f for f in table.fields}
        hand = self.hand_made(entity_id)
        for name, raw in fields.items():
            name = str(name)
            if name == TITLE:
                current: Any = self.conn.scalar(select(Entity.title).where(Entity.id == entity_id))
                value: Any = str(raw)
                label = table.entity.title_label
            else:
                info = infos.get(name)
                if info is None or not info.spec.editable or info.spec.mirrors is not None:
                    if info is None:
                        self.problems.append(
                            f"{self.describe(entity_id)}: no field {name!r} in this keep."
                        )
                    continue  # a field that mirrors links comes back with the links
                try:
                    value = _value(info.type, raw)
                except ValueError:
                    self.problems.append(
                        f"{self.describe(entity_id)}: {info.spec.label} {raw!r} isn't valid."
                    )
                    continue
                current = self.conn.scalar(
                    select(table.table.c[name]).where(table.table.c.id == entity_id)
                )
                label = info.spec.label
            if plain_value(current) == plain_value(value):
                continue
            if name in hand and not self.conflict(entity_id, label, current, value):
                continue
            self.touch(entity_id)
            try:
                edit_field(self.conn, self.schema, entity_id, name, value)
            except FieldEditError as e:
                self.problems.append(f"{self.describe(entity_id)}: {e}")
                continue
            self.did(entity_id, label)

    def hand_made(self, entity_id: int) -> dict[str, FieldSource]:
        """The item's fields' provenance, by key."""
        return {
            f: s
            for f, s in self.conn.execute(
                select(FieldProvenance.field, FieldProvenance.source).where(
                    FieldProvenance.entity_id == entity_id
                )
            )
            if s == FieldSource.USER
        }

    def extra(self, entity_id: int, extra: Any) -> None:
        if not isinstance(extra, dict):
            return
        current = dict(self.conn.scalar(select(Entity.extra).where(Entity.id == entity_id)) or {})
        sources = {
            r.field: r.source
            for r in self.conn.execute(
                select(FieldProvenance.field, FieldProvenance.source).where(
                    FieldProvenance.entity_id == entity_id
                )
            )
        }
        for name, raw in extra.items():
            name, value = str(name), str(plain_value(raw))
            here = current.get(name)
            if here is not None and str(plain_value(here)) == value:
                continue
            by_hand = sources.get(EXTRA + name, FieldSource.USER) == FieldSource.USER
            if (
                here is not None
                and by_hand
                and not self.conflict(entity_id, f"Extra field {name!r}", here, value)
            ):
                continue
            self.touch(entity_id)
            try:
                edit_extra(self.conn, self.schema, entity_id, name, value, new=here is None)
            except FieldEditError as e:
                self.problems.append(f"{self.describe(entity_id)}: {e}")
                continue
            self.did(entity_id, "Extra fields")

    def own(self, entity_id: int, values: Any) -> None:
        if not isinstance(values, dict):
            return
        type_id = str(self.conn.scalar(select(Entity.type).where(Entity.id == entity_id)))
        fields = self.own_fields.get(type_id, {})
        current = raw_values(self.conn, [entity_id]).get(entity_id, {})
        hand = self.hand_made(entity_id)
        for name, raw in values.items():
            own = fields.get(str(name))
            if own is None:
                self.problems.append(
                    f"{self.describe(entity_id)}: this keep has no field {str(name)!r} of its "
                    "own for it (Keep configuration → Fields)."
                )
                continue
            try:
                value = _value(own.python_type, raw)
            except ValueError:
                self.problems.append(
                    f"{self.describe(entity_id)}: {own.label} {raw!r} isn't valid."
                )
                continue
            here = current.get(own.name)
            if plain_value(here) == plain_value(value):
                continue
            if (
                here is not None
                and own.key in hand
                and not self.conflict(entity_id, own.label, here, value)
            ):
                continue
            self.touch(entity_id)
            try:
                edit_own_field(self.conn, self.schema, own, entity_id, value)
            except FieldEditError as e:
                self.problems.append(f"{self.describe(entity_id)}: {e}")
                continue
            self.did(entity_id, own.label)

    def note(self, entity_id: int, body: Any) -> None:
        if not isinstance(body, str) or not body.strip():
            return
        row = self.conn.execute(
            select(EntityNote.body, EntityNote.resource_id, EntityNote.folder_id).where(
                EntityNote.entity_id == entity_id
            )
        ).first()
        if row is not None and (row.resource_id is not None or row.folder_id is not None):
            self.problems.append(
                f"{self.describe(entity_id)}: its note is a file here; the typed note is left out."
            )
            return
        here = row.body if row is not None else None
        if here is not None and here.strip() == body.strip():
            return
        if here is not None and not self.conflict(entity_id, "the note", here, body):
            return
        self.touch(entity_id)
        edit_note(self.conn, self.schema, entity_id, body)
        self.did(entity_id, "note")

    def note_removals(self, entity_id: int, keys: Any) -> None:
        wanted = [str(k).casefold() for k in _list(keys)]
        have = set(
            self.conn.scalars(
                select(NoteKeyRemoval.key).where(NoteKeyRemoval.entity_id == entity_id)
            )
        )
        new = [k for k in dict.fromkeys(wanted) if k not in have]
        if not new:
            return
        self.touch(entity_id)
        extra = dict(self.conn.scalar(select(Entity.extra).where(Entity.id == entity_id)) or {})
        for key in new:
            name = next((n for n in extra if n.casefold() == key), None)
            if name is not None:  # from its note: removing it records the removal
                edit_extra(self.conn, self.schema, entity_id, name, None)
            still = self.conn.scalar(
                select(NoteKeyRemoval.key).where(
                    NoteKeyRemoval.entity_id == entity_id, NoteKeyRemoval.key == key
                )
            )
            if still is None:
                self.conn.execute(insert(NoteKeyRemoval).values(entity_id=entity_id, key=key))
        self.did(entity_id, "note fields removed")

    def picture(self, entity_id: int, path: Any) -> None:
        if not path:
            return
        rid = self.resource(path)
        if rid is None:
            return
        here = self.conn.scalar(
            select(ItemPicture.resource_id).where(
                ItemPicture.entity_id == entity_id, ItemPicture.source == HAND_MADE
            )
        )
        if here == rid:
            return
        if here is not None:
            shown = self.conn.scalar(select(Resource.relpath).where(Resource.id == here))
            if not self.conflict(entity_id, "its picture", shown, path):
                return
        try:
            change: PictureChange = set_picture(
                self.conn, entity_id, rid, f"Set {self.describe(entity_id)}'s picture"
            )
        except PictureError as e:
            self.problems.append(f"{self.describe(entity_id)}: {e}")
            return
        self.steps.append(change)
        self.did(entity_id, "picture")

    def links(self, entity_id: int, links: Any) -> None:
        for link in _list(links):
            if not isinstance(link, dict):
                continue
            role = str(link.get("role") or "")
            rid = self.resource(link.get("file"))
            if rid is None:
                continue
            by_user = self.conn.scalar(
                select(EntityResource.by_user).where(
                    EntityResource.entity_id == entity_id,
                    EntityResource.resource_id == rid,
                    EntityResource.role == role,
                )
            )
            if by_user:
                continue
            self.touch(entity_id)
            try:
                link_files(self.conn, self.schema, entity_id, [rid], role)
            except LinkError as e:
                self.problems.append(f"{self.describe(entity_id)}: {e}")
                continue
            self.did(entity_id, "files")

    def containers(self, entity_id: int, entry: dict[str, Any]) -> None:
        for added, key in ((True, "put in"), (False, "taken out of")):
            for ref in _list(entry.get(key)):
                parent = self.find(ref)
                if parent is None:
                    continue
                recorded = self.conn.scalar(
                    select(UserContains.added).where(
                        UserContains.parent_id == parent, UserContains.child_id == entity_id
                    )
                )
                if recorded is added:
                    continue
                inside = self.conn.scalar(
                    select(EntityContains.child_id).where(
                        EntityContains.parent_id == parent, EntityContains.child_id == entity_id
                    )
                )
                self.touch(entity_id, parent)
                try:
                    if added and inside is None:
                        contain_by_hand(self.conn, self.schema, parent, [entity_id])
                    elif not added and inside is not None:
                        uncontain_by_hand(self.conn, self.schema, parent, [entity_id])
                    else:
                        record_contains(self.conn, parent, entity_id, added=added)
                except ContainerError as e:
                    self.problems.append(f"{self.describe(entity_id)}: {e}")
                    continue
                self.did(entity_id, "containers")

    def relations(self, entity_id: int, entry: dict[str, Any]) -> None:
        for added, key in ((True, "related"), (False, "unrelated")):
            lists = entry.get(key)
            if not isinstance(lists, dict):
                continue
            for name, refs in lists.items():
                rel = self.schema.relationships.get(str(name))
                if rel is None:
                    self.problems.append(
                        f"{self.describe(entity_id)}: no relationship {str(name)!r} in this keep."
                    )
                    continue
                for ref in _list(refs):
                    other = self.find(ref)
                    if other is None:
                        continue
                    self.relate(rel.table, str(name), entity_id, other, added)

    def relate(self, table: Any, name: str, a_id: int, b_id: int, added: bool) -> None:
        recorded = self.conn.scalar(
            select(UserRelation.added).where(
                UserRelation.name == name, UserRelation.a_id == a_id, UserRelation.b_id == b_id
            )
        )
        if recorded is added:
            return
        present = self.conn.scalar(
            select(table.c.a_id).where(table.c.a_id == a_id, table.c.b_id == b_id)
        )
        self.touch(a_id, b_id)
        try:
            if added and present is None:
                add_related(self.conn, self.schema, name, a_id, b_id, side="a")
            elif not added and present is not None:
                remove_related(self.conn, self.schema, name, a_id, [b_id], side="a")
            else:
                record_relation(self.conn, name, a_id, b_id, added=added)
        except RelationError as e:
            self.problems.append(f"{self.describe(a_id)}: {e}")
            return
        self.did(a_id, "relationships")

    def orders(self, b_id: int, orders: Any) -> None:
        if not isinstance(orders, dict):
            return
        for name, refs in orders.items():
            rel = self.schema.relationships.get(str(name))
            if rel is None or not rel.relationship.ordered:
                continue
            table = rel.table
            current: list[int] = list(
                self.conn.scalars(
                    select(table.c.a_id)
                    .where(table.c.b_id == b_id)
                    .order_by(table.c.position, table.c.a_id)
                )
            )
            listed = [a for r in _list(refs) if (a := self.find(r)) is not None and a in current]
            new = list(dict.fromkeys(listed)) + [a for a in current if a not in listed]
            ordered = self.conn.scalar(
                select(UserOrder.b_id).where(UserOrder.name == str(name), UserOrder.b_id == b_id)
            )
            positions: list[int | None] = list(
                self.conn.scalars(
                    select(table.c.position)
                    .where(table.c.b_id == b_id)
                    .order_by(table.c.position, table.c.a_id)
                )
            )
            if ordered is not None and new == current and positions == list(range(len(new))):
                continue
            self.touch(b_id)
            for position, a_id in enumerate(new):
                self.conn.execute(
                    update(table)
                    .where(table.c.a_id == a_id, table.c.b_id == b_id)
                    .values(position=position)
                )
            self.conn.execute(
                sqlite_insert(UserOrder)
                .values(name=str(name), b_id=b_id, at=utcnow())
                .on_conflict_do_nothing(index_elements=["name", "b_id"])
            )
            self.did(b_id, "order")

    # --- the keep's records ---

    def saved_searches(self, searches: list[Any]) -> None:
        tree = TagTree.load(self.conn)
        for entry in searches:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            name = str(entry["name"])
            try:
                stored = self.stored_search(tree, entry.get("search"))
                definition = SavedDefinition.from_json(stored)
            except (SavedSearchError, ValueError) as e:
                self.problems.append(f"The saved search {name!r}: {e}")
                continue
            data = definition.to_json()
            existing = find_by_name(self.conn, name)
            if existing is not None:
                here = self.conn.scalar(
                    select(SavedSearch.definition).where(SavedSearch.id == existing)
                )
                same = SavedDefinition.from_json(here).to_json() == data if here else False
                if same:
                    continue
                self.conflicts.append(
                    f"The saved search {name!r} is different here than in the file"
                )
                if not self.replace:
                    continue
            try:
                self.steps.append(save_search(self.conn, name, definition, replace=existing))
            except SavedSearchError as e:
                self.problems.append(str(e))
                continue
            self.records.append(f"Saved search {name!r}")

    def stored_search(self, tree: TagTree, data: Any) -> Any:
        """A search as written back as stored: tag paths as ids, items as ids."""
        if not isinstance(data, dict):
            return data
        stored: dict[str, Any] = {}
        for key, value in data.items():
            if key in ("include", "exclude") and isinstance(value, list):
                ids = []
                for path in value:
                    names = _tag_names(path)
                    tag_id = resolve_path(tree, names) if names else None
                    if tag_id is None:
                        raise ValueError(f"no tag {path!r} in this keep")
                    ids.append(tag_id)
                stored[key] = ids
            elif key == "within" and isinstance(value, dict):
                found = self.find(value)
                if found is None:
                    raise ValueError(f"its item {value.get('title')!r} isn't in this keep")
                stored[key] = found
            elif key == "related" and isinstance(value, list) and len(value) == 2:
                found = self.find(value[1]) if isinstance(value[1], dict) else None
                if found is None:
                    raise ValueError("its related item isn't in this keep")
                stored[key] = [value[0], found]
            elif isinstance(value, dict):
                stored[key] = self.stored_search(tree, value)
            else:
                stored[key] = value
        return stored

    def keywords(self, keywords: Any) -> None:
        if not isinstance(keywords, dict):
            return
        keys = [str(k) for k in _list(keywords.get("ignored"))]
        change = set_ignored(self.conn, keys, True)
        if change.keys:
            self.steps.append(change)
            self.records.append(change.label)

    def archives(self, archives: list[Any]) -> None:
        wanted: dict[int, tuple[str, dict[str, bool]]] = {}
        for entry in archives:
            if not isinstance(entry, dict):
                continue
            rid = self.resource(entry)
            mode = str(entry.get("read") or "")
            if rid is None:
                continue
            if mode not in tuple(ArchiveMode):
                self.problems.append(f"{entry.get('path')}: {mode!r} isn't a way of reading it.")
                continue
            listed = entry.get("members")
            choices = (
                {str(p): bool(r) for p, r in listed.items()} if isinstance(listed, dict) else {}
            )
            wanted[rid] = (mode, choices)
        if not wanted:
            return
        ids = sorted(wanted)
        before = _archive_rows(self.conn, ids)
        modes = dict(before[0])
        members: dict[int, dict[str, bool | None]] = {}
        for a, p, r in before[1]:
            members.setdefault(a, {})[p] = r
        changed = []
        for rid, (mode, choices) in wanted.items():
            decided = {p: r for p, r in members.get(rid, {}).items() if r is not None}
            if modes.get(rid) == mode and decided == choices:
                continue
            if modes.get(rid) is not None and modes.get(rid) != mode:
                path = self.conn.scalar(select(Resource.relpath).where(Resource.id == rid))
                self.conflicts.append(f"{path}: read {modes.get(rid)!r} here, {mode!r} in the file")
                if not self.replace:
                    continue
            _set_mode(self.conn, rid, mode)
            self.conn.execute(
                delete(ArchiveMemberChoice).where(ArchiveMemberChoice.archive_id == rid)
            )
            if choices:
                self.conn.execute(
                    insert(ArchiveMemberChoice),
                    [{"archive_id": rid, "path": p, "read": r} for p, r in choices.items()],
                )
            changed.append(rid)
        if changed:
            n = len(changed)
            label = f"Chose what to read in {n} archive{'' if n == 1 else 's'}"
            self.steps.append(
                ArchiveChange(label, tuple(ids), before, _archive_rows(self.conn, ids))
            )
            self.records.append(label + " (read at the next scan)")

    def dismissed(self, dismissed: Any) -> None:
        if not isinstance(dismissed, dict):
            return
        for name, key in ((UNLINKED, "unlinked files"), (UNTAGGED, "untagged items")):
            entries = _list(dismissed.get(key))
            ids = [
                i
                for e in entries
                if (i := (self.resource(e) if name == UNLINKED else self.find(e))) is not None
            ]
            have = set(
                self.conn.scalars(
                    select(TriageDismissal.target_id).where(
                        TriageDismissal.list == name, TriageDismissal.target_id.in_(ids)
                    )
                )
            )
            new = [i for i in dict.fromkeys(ids) if i not in have]
            if new:
                change = dismiss(self.conn, name, new)
                self.steps.append(change)
                self.records.append(f"{change.label} from Triage")
        self.not_duplicates(dismissed)
        self.picture_rules(_list(dismissed.get("picture problems")))

    def not_duplicates(self, dismissed: Mapping[str, Any]) -> None:
        entries: list[tuple[str, str, str]] = []
        for key in _list(dismissed.get("identical files")):
            key = str(key)
            try:
                fingerprint = bytes.fromhex(key)
            except ValueError:
                continue
            ids = sorted(
                self.conn.scalars(select(Resource.id).where(Resource.fingerprint == fingerprint))
            )
            if len(ids) > 1:
                entries.append((EXACT, key, ",".join(str(i) for i in ids)))
        for pair in _list(dismissed.get("similar items")):
            found = [self.find(r) for r in _list(pair)]
            if len(found) == 2 and found[0] is not None and found[1] is not None:
                low, high = sorted((found[0], found[1]))
                entries.append((SIMILAR, f"{low}:{high}", ""))
        for pair in _list(dismissed.get("folder pairs")):
            sides = [self.folder(s) for s in _list(pair)]
            if len(sides) == 2 and sides[0] and sides[1]:
                key = "\n".join(f"{root}:{path}" for root, path in (sides[0], sides[1]))
                entries.append((FOLDERS, key, ""))
        have = set(
            self.conn.execute(
                select(DedupeDismissal.kind, DedupeDismissal.key, DedupeDismissal.marker)
            ).all()
        )
        new = [e for e in dict.fromkeys(entries) if e not in have]
        if new:
            change = set_not_duplicate(self.conn, new)
            self.steps.append(change)
            self.records.append(f"{change.label} in Duplicates")

    def picture_rules(self, rules: list[Any]) -> None:
        wanted: set[PictureRule] = set()
        problems = None
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if "value" in rule:
                wanted.add(PictureRule(VALUE, value=str(rule["value"])))
            elif "type" in rule:
                type_id = self.kinds.get(search_key(str(rule["type"])))
                if type_id is not None:
                    wanted.add(PictureRule(TYPE, type=type_id))
            elif "item" in rule:
                entity_id = self.find(rule["item"])
                if entity_id is None:
                    continue
                if problems is None:
                    problems = picture_problems(self.conn, lambda root: None)
                wanted.update(rule_for(p, ITEM) for p in problems if p.entity_id == entity_id)
        new = frozenset(wanted) - picture_rules(self.conn)
        if new:
            n = len(new)
            change = PictureIgnoreChange(
                f"Ignore {n} picture problem{'' if n == 1 else 's'}", added=new
            )
            restore_picture_ignores(self.conn, change, forward=True)
            self.steps.append(change)
            self.records.append(f"{change.label} in Triage")


def _value(kind: type, raw: Any) -> Any:
    """A value as the export wrote it, as a field of ``kind`` takes it. Raises
    :class:`ValueError` for one that can't be."""
    if raw is None:
        return None
    if kind is datetime:
        if isinstance(raw, datetime):
            found = raw
        elif isinstance(raw, date):
            found = datetime(raw.year, raw.month, raw.day)
        else:
            found = datetime.fromisoformat(str(raw))
        return found if found.tzinfo else found.replace(tzinfo=UTC)  # stored as UTC
    if kind is date:
        if isinstance(raw, datetime):
            return raw.date()
        return raw if isinstance(raw, date) else date.fromisoformat(str(raw))
    if kind is float and isinstance(raw, int) and not isinstance(raw, bool):
        return float(raw)
    if kind is int and isinstance(raw, str):
        return int(raw)
    if kind is str and not isinstance(raw, str):
        return str(plain_value(raw))
    return raw


def _set_mode(conn: Connection, rid: int, mode: str | None) -> None:
    statement = sqlite_insert(ArchiveRead).values(
        resource_id=rid,
        mode=ArchiveMode(mode) if mode else None,
        listed_size=None,
        listed_mtime_ns=None,
    )
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=[ArchiveRead.resource_id],
            set_={"mode": statement.excluded.mode, "listed_size": None, "listed_mtime_ns": None},
        )
    )


def _archive_rows(
    conn: Connection, ids: Sequence[int]
) -> tuple[tuple[tuple[int, str | None], ...], tuple[tuple[int, str, bool | None], ...]]:
    reads = tuple(
        (rid, str(mode) if mode else None)
        for rid, mode in conn.execute(
            select(ArchiveRead.resource_id, ArchiveRead.mode)
            .where(ArchiveRead.resource_id.in_(ids))
            .order_by(ArchiveRead.resource_id)
        )
    )
    members = tuple(
        (a, p, r)
        for a, p, r in conn.execute(
            select(
                ArchiveMemberChoice.archive_id, ArchiveMemberChoice.path, ArchiveMemberChoice.read
            )
            .where(ArchiveMemberChoice.archive_id.in_(ids))
            .order_by(ArchiveMemberChoice.archive_id, ArchiveMemberChoice.path)
        )
    )
    return reads, members


def plan_summary(plan: KeepImportPlan) -> str:
    """A one-line account of an import, for the status bar."""
    parts = []
    tags = plan.tags
    if tags.new:
        parts.append(f"{len(tags.new):,} new tag{'' if len(tags.new) == 1 else 's'}")
    if tags.updated:
        parts.append(f"{len(tags.updated):,} tags updated")
    if plan.changed:
        n = len(plan.changed)
        parts.append(f"{n:,} item{'' if n == 1 else 's'} changed")
    if plan.records:
        n = len(plan.records)
        parts.append(f"{n:,} other record{'' if n == 1 else 's'}")
    text = ", ".join(parts) if parts else "nothing to change"
    if plan.not_found:
        text += f"; {len(plan.not_found):,} not found"
    return f"Imported the keep export: {text}."
