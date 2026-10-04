"""Tag operations with a session undo/redo history (DESIGN.md §7).

Every operation runs through the DB writer as one transaction that also records a
:class:`TagChange`: full snapshots of the (small) ``tag`` and ``tag_alias`` tables before and
after, plus the ``entity_tag`` rows the operation removed and added within its scope. Undo
restores the "before" tables and reverses the ``entity_tag`` delta; redo does the opposite.
Neither re-runs the operation. Tagging done between an operation and its undo is untouched,
because only the operation's own delta is reversed.

Field edits on detail pages share the same history (:meth:`TagService.edit_field`): one
step each, recorded as a :class:`~tagalot.core.fields.FieldChange`.
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, delete, insert, select, tuple_, update

from tagalot.core.actions import ActionChange, restore_action
from tagalot.core.fields import (
    ExtraChange,
    FieldChange,
    edit_extra,
    edit_field,
    restore_extra,
    restore_field,
)
from tagalot.core.keywords import (
    IgnoreChange,
    apply_file_tags,
    forget_removals,
    remember_removals,
    restore_ignored,
    set_ignored,
)
from tagalot.core.merge import MergeChange, restore_merge
from tagalot.core.models import Entity, EntityTag, FileTagRemoval, Tag, TagAlias
from tagalot.core.not_duplicates import NotDuplicateChange, restore_not_duplicates
from tagalot.core.reextract import ReextractChange, restore_entities
from tagalot.core.saved_searches import SavedChange, restore_saved
from tagalot.core.tags import (
    PATH_SEPARATOR,
    DeleteMode,
    TagTree,
    TagTreeCache,
    add_alias,
    add_tag,
    add_tag_path,
    delete_tag,
    merge_tags,
    remove_alias,
    rename_tag,
    reparent_tag,
    set_tag_color,
    set_tag_description,
    set_tag_types,
    tag_entities,
    untag_entities,
)
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.triage import DismissChange, restore_dismissals
from tagalot.core.writer import DbWriter

logger = logging.getLogger(__name__)

MAX_HISTORY = 100
"""Undo steps kept per session."""

TagRow = tuple[int, int | None, str, str | None, int, str | None, str | None]
"""``(id, parent_id, name, color, sort_order, description, types)``."""
EntityTagRow = tuple[int, int, datetime]
"""``(entity_id, tag_id, added_at)``."""


@dataclass(frozen=True)
class TagChange:
    """Everything needed to undo and redo one tag operation."""

    label: str
    """What the user did, e.g. ``Merge 'Bebop' into 'Jazz'``."""
    tags_before: frozenset[TagRow]
    tags_after: frozenset[TagRow]
    aliases_before: frozenset[tuple[int, str]]
    aliases_after: frozenset[tuple[int, str]]
    removed: frozenset[EntityTagRow]
    """The user's ``entity_tag`` rows the operation deleted (file tags are derived, #294)."""
    added: frozenset[EntityTagRow]
    """The user's ``entity_tag`` rows the operation created."""
    removals_added: frozenset[tuple[int, int]] = frozenset()
    """``(entity, tag)``: file tags the user removed by hand, remembered (#294)."""
    removals_dropped: frozenset[tuple[int, int]] = frozenset()
    """Removal records dropped because the user put the tag back by hand."""


LinkDelta = tuple[
    frozenset[EntityTagRow],
    frozenset[EntityTagRow],
    frozenset[tuple[int, int]],
    frozenset[tuple[int, int]],
]
"""What a tagging operation did: the user's rows added and removed, removal records added,
and removal records dropped."""


Step = (
    TagChange
    | FieldChange
    | ExtraChange
    | ReextractChange
    | ActionChange
    | DismissChange
    | IgnoreChange
    | MergeChange
    | NotDuplicateChange
    | SavedChange
)
"""One entry in the undo history."""


@dataclass(frozen=True)
class Applied:
    """What tagging did: rows added, and ``(entity, tag, entity type)`` pairs a tag's types
    didn't allow (#135)."""

    added: int
    skipped: tuple[tuple[int, int, str], ...] = ()


class TagService:
    """Runs tag operations (and field edits, with ``schema``) through the DB writer, records
    them for undo, and refreshes the tag tree cache."""

    def __init__(
        self, writer: DbWriter, cache: TagTreeCache, schema: ThemeSchema | None = None
    ) -> None:
        self.writer = writer
        self.cache = cache
        self.schema = schema
        self._undo: list[Step] = []
        self._redo: list[Step] = []

    # --- operations ---

    def add(self, parent_id: int | None, name: str, color: str | None = None) -> int:
        return self._record(
            lambda tree: f"Add tag {name.strip()!r}",
            lambda tree: (),
            lambda conn: add_tag(conn, parent_id, name, color),
        )

    def add_path(self, names: Sequence[str]) -> int:
        """Create the tag at ``names`` (top first) and any missing parents, as one step."""
        path = PATH_SEPARATOR.join(n.strip() for n in names)
        return self._record(
            lambda tree: f"Add tag {path!r}",
            lambda tree: (),
            lambda conn: add_tag_path(conn, names),
        )

    def rename(self, tag_id: int, name: str) -> None:
        self._record(
            lambda tree: f"Rename {_name(tree, tag_id)!r} to {name.strip()!r}",
            lambda tree: (),
            lambda conn: rename_tag(conn, tag_id, name),
        )

    def reparent(self, tag_id: int, new_parent_id: int | None) -> None:
        self._record(
            lambda tree: f"Move {_name(tree, tag_id)!r}",
            lambda tree: (),
            lambda conn: reparent_tag(conn, tag_id, new_parent_id),
        )

    def merge(self, source_id: int, target_id: int) -> None:
        self._record(
            lambda tree: f"Merge {_name(tree, source_id)!r} into {_name(tree, target_id)!r}",
            lambda tree: _subtrees(tree, source_id, target_id),
            lambda conn: merge_tags(conn, source_id, target_id),
        )

    def delete(self, tag_id: int, mode: DeleteMode | None = None) -> None:
        self._record(
            lambda tree: f"Delete {_name(tree, tag_id)!r}",
            lambda tree: _subtrees(tree, tag_id),
            lambda conn: delete_tag(conn, tag_id, mode),
        )

    def set_color(self, tag_id: int, color: str | None) -> None:
        self._record(
            lambda tree: f"Change the color of {_name(tree, tag_id)!r}",
            lambda tree: (),
            lambda conn: set_tag_color(conn, tag_id, color),
        )

    def set_types(self, tag_id: int, types: Iterable[str] | None) -> int:
        """Limit a tag (and its sub-tags) to some item types, or ``None`` for every type,
        taking it off items it no longer allows; one undoable step. Returns how many uses
        were removed (#135)."""
        chosen = None if types is None else frozenset(types)
        return self._record(
            lambda tree: f"Change the types {_name(tree, tag_id)!r} applies to",
            lambda tree: _subtrees(tree, tag_id),
            lambda conn: set_tag_types(conn, tag_id, chosen),
        )

    def set_description(self, tag_id: int, description: str | None) -> None:
        self._record(
            lambda tree: f"Change the description of {_name(tree, tag_id)!r}",
            lambda tree: (),
            lambda conn: set_tag_description(conn, tag_id, description),
        )

    # --- tagging entities ---

    def apply(self, entity_ids: Iterable[int], tag_ids: Iterable[int]) -> int:
        """Tag entities (undoable); returns how many ``entity_tag`` rows were added."""
        return self.apply_counted(entity_ids, tag_ids).added

    def apply_counted(self, entity_ids: Iterable[int], tag_ids: Iterable[int]) -> Applied:
        """Tag entities (undoable), reporting what a tag's types left out (#135)."""
        entities, tags = frozenset(entity_ids), frozenset(tag_ids)
        skipped: list[tuple[int, int, str]] = []

        def op(conn: Connection) -> LinkDelta:
            added = tag_entities(conn, entities, tags, skipped)
            # Putting a file's tag back by hand forgets that it was removed (#294).
            dropped = forget_removals(conn, {(e, t) for e, t, _ in added})
            return added, frozenset(), frozenset(), dropped

        added = self._record_links(
            lambda tree: f"Tag {_items(len(entities))} with {_names(tree, tags)}", op
        )
        return Applied(added, tuple(skipped))

    def remove(self, entity_ids: Iterable[int], tag_ids: Iterable[int]) -> int:
        """Untag entities (undoable); returns how many ``entity_tag`` rows were removed."""
        entities, tags = frozenset(entity_ids), frozenset(tag_ids)

        def op(conn: Connection) -> LinkDelta:
            removed = untag_entities(conn, entities, tags)
            # A tag the item's files give stays off from now on (#294).
            kept_off = remember_removals(conn, TagTree.load(conn), {(e, t) for e, t, _ in removed})
            return frozenset(), removed, kept_off, frozenset()

        return self._record_links(
            lambda tree: f"Remove {_names(tree, tags)} from {_items(len(entities))}", op
        )

    def add_alias(self, tag_id: int, alias: str, *, allow_name: bool = False) -> None:
        self._record(
            lambda tree: f"Add alias {alias.strip()!r} to {_name(tree, tag_id)!r}",
            lambda tree: (),
            lambda conn: add_alias(conn, tag_id, alias, allow_name=allow_name),
        )

    def map_keywords(self, tag_id: int, keywords: Iterable[str]) -> None:
        """Tie file keywords to a tag: each becomes its alias, so every item whose files give
        one gets the tag (#295). One undo step."""
        words = list(keywords)
        what = repr(words[0]) if len(words) == 1 else f"{len(words)} keywords"

        def op(conn: Connection) -> None:
            for keyword in words:
                add_alias(conn, tag_id, keyword, allow_name=True)

        self._record(lambda tree: f"Map {what} to {_name(tree, tag_id)!r}", lambda tree: (), op)

    def remove_alias(self, tag_id: int, alias: str) -> None:
        self._record(
            lambda tree: f"Remove alias {alias.strip()!r} from {_name(tree, tag_id)!r}",
            lambda tree: (),
            lambda conn: remove_alias(conn, tag_id, alias),
        )

    def edit_field(self, entity_id: int, name: str, value: Any) -> FieldChange:
        """Set a field (or ``"title"``) by hand: provenance ``user``, one undo step.
        Raises :class:`~tagalot.core.fields.FieldEditError` for a value that doesn't fit."""
        schema = self.schema
        if schema is None:
            raise RuntimeError("field edits need the keep's schema")
        change = self.writer.run(lambda conn: edit_field(conn, schema, entity_id, name, value))
        if change.before != change.after or change.source_before != change.source_after:
            self._push(change)
        return change

    def edit_extra(
        self, entity_id: int, name: str, value: str | None, *, new: bool = False
    ) -> ExtraChange:
        """Add, change, or remove one of an entity's extra fields; one undo step."""
        schema = self.schema
        if schema is None:
            raise RuntimeError("extra fields need the keep's schema")
        change = self.writer.run(
            lambda conn: edit_extra(conn, schema, entity_id, name, value, new=new)
        )
        if change.before != change.after:
            self._push(change)
        return change

    def ignore_keywords(self, keys: Iterable[str], ignored: bool = True) -> int:
        """Ignore file keywords, or stop ignoring them (#295); one undo step. Returns how
        many changed."""
        chosen = frozenset(keys)
        change = self.writer.run(lambda conn: set_ignored(conn, chosen, ignored))
        if change.keys:
            self._push(change)
        return len(change.keys)

    def record(self, step: Step) -> None:
        """Add a step done elsewhere (a re-read of files) to the history."""
        self._push(step)

    # --- history ---

    @property
    def undo_label(self) -> str | None:
        """What :meth:`undo` would undo, for the Edit menu; ``None`` if nothing."""
        return self._undo[-1].label if self._undo else None

    @property
    def redo_label(self) -> str | None:
        return self._redo[-1].label if self._redo else None

    def undo(self) -> str | None:
        """Undo the most recent operation; returns its label, or ``None`` if there is none."""
        if not self._undo:
            return None
        change = self._undo.pop()
        self._replay(change, forward=False)
        self._redo.append(change)
        return change.label

    def redo(self) -> str | None:
        """Redo the most recently undone operation; returns its label, or ``None``."""
        if not self._redo:
            return None
        change = self._redo.pop()
        self._replay(change, forward=True)
        self._undo.append(change)
        return change.label

    def clear_history(self) -> None:
        self._undo.clear()
        self._redo.clear()

    # --- internals ---

    def _push(self, step: Step) -> None:
        self._undo.append(step)
        del self._undo[:-MAX_HISTORY]
        self._redo.clear()

    def _replay(self, step: Step, *, forward: bool) -> None:
        schema = self.schema
        if isinstance(step, FieldChange):
            assert schema is not None  # field edits are only recorded with a schema
            self.writer.run(lambda conn: restore_field(conn, schema, step, forward=forward))
        elif isinstance(step, ExtraChange):
            assert schema is not None
            self.writer.run(lambda conn: restore_extra(conn, schema, step, forward=forward))
        elif isinstance(step, ReextractChange):
            assert schema is not None
            self.writer.run(lambda conn: restore_entities(conn, schema, step, forward=forward))
        elif isinstance(step, DismissChange):
            self.writer.run(lambda conn: restore_dismissals(conn, step, forward=forward))
        elif isinstance(step, IgnoreChange):
            self.writer.run(lambda conn: restore_ignored(conn, step, forward=forward))
        elif isinstance(step, ActionChange):
            assert schema is not None
            self.writer.run(lambda conn: restore_action(conn, schema, step, forward=forward))
        elif isinstance(step, SavedChange):
            self.writer.run(lambda conn: restore_saved(conn, step, forward=forward))
        elif isinstance(step, NotDuplicateChange):
            self.writer.run(lambda conn: restore_not_duplicates(conn, step, forward=forward))
        elif isinstance(step, MergeChange):
            assert schema is not None
            self.writer.run(lambda conn: restore_merge(conn, schema, step, forward=forward))
        else:
            self._apply(lambda conn: _restore(conn, step, forward=forward))

    def _record[T](
        self,
        label: Callable[[TagTree], str],
        scope: Callable[[TagTree], Iterable[int]],
        op: Callable[[Connection], T],
    ) -> T:
        def job(conn: Connection) -> tuple[T, TagChange]:
            tree = TagTree.load(conn)
            tag_ids = frozenset(scope(tree))
            tags_before, aliases_before = _tag_tables(conn)
            links_before = _links(conn, tag_ids)
            removals_before = _removals(conn, tag_ids)
            result = op(conn)
            apply_file_tags(conn)  # a rename, alias, or type change changes what matches
            tags_after, aliases_after = _tag_tables(conn)
            links_after = _links(conn, tag_ids)
            removals_after = _removals(conn, tag_ids)
            change = TagChange(
                label=label(tree),
                tags_before=tags_before,
                tags_after=tags_after,
                aliases_before=aliases_before,
                aliases_after=aliases_after,
                removed=links_before - links_after,
                added=links_after - links_before,
                # A merge moves removal records; a delete takes them with the tag (#294).
                removals_added=removals_after - removals_before,
                removals_dropped=removals_before - removals_after,
            )
            return result, change

        result, change = self._apply(job)
        self._push(change)
        return result

    def _record_links(
        self,
        label: Callable[[TagTree], str],
        op: Callable[[Connection], "LinkDelta"],
    ) -> int:
        """Run a tagging operation, recording exactly the rows it added and removed. Unlike
        :meth:`_record`, it never reads every use of a tag, so tagging stays cheap for tags
        on tens of thousands of entities. A change that changed nothing isn't recorded."""

        def job(conn: Connection) -> TagChange:
            tree = TagTree.load(conn)
            tags, aliases = _tag_tables(conn)
            added, removed, kept_off, dropped = op(conn)
            return TagChange(
                label(tree), tags, tags, aliases, aliases, removed, added, kept_off, dropped
            )

        change = self.writer.run(job)  # the tag tree is unchanged: no cache refresh
        if change.added or change.removed:
            self._push(change)
        return len(change.added) + len(change.removed)

    def _apply[T](self, job: Callable[[Connection], T]) -> T:
        try:
            return self.writer.run(job)
        finally:
            self.cache.invalidate()


def _name(tree: TagTree, tag_id: int) -> str:
    return tree.node(tag_id).name if tag_id in tree else f"#{tag_id}"


def _items(count: int) -> str:
    return "1 item" if count == 1 else f"{count:,} items"


def _names(tree: TagTree, tag_ids: frozenset[int]) -> str:
    names = sorted(repr(_name(tree, t)) for t in tag_ids)
    return ", ".join(names) if len(names) <= 3 else f"{len(names)} tags"


def _subtrees(tree: TagTree, *tag_ids: int) -> frozenset[int]:
    return tree.expand(t for t in tag_ids if t in tree)


def _tag_tables(conn: Connection) -> tuple[frozenset[TagRow], frozenset[tuple[int, str]]]:
    tags = conn.execute(
        select(
            Tag.id, Tag.parent_id, Tag.name, Tag.color, Tag.sort_order, Tag.description, Tag.types
        )
    )
    aliases = conn.execute(select(TagAlias.tag_id, TagAlias.alias))
    return frozenset(tuple(r) for r in tags), frozenset(tuple(r) for r in aliases)  # type: ignore[misc]


def _links(conn: Connection, tag_ids: frozenset[int]) -> frozenset[EntityTagRow]:
    if not tag_ids:
        return frozenset()
    rows = conn.execute(
        select(EntityTag.entity_id, EntityTag.tag_id, EntityTag.added_at).where(
            EntityTag.tag_id.in_(tag_ids),
            EntityTag.by_file.is_(False),  # the user's own
        )
    )
    return frozenset((e, t, a) for e, t, a in rows)


def _removals(conn: Connection, tag_ids: frozenset[int]) -> frozenset[tuple[int, int]]:
    """Removal records (#294) for these tags."""
    if not tag_ids:
        return frozenset()
    rows = conn.execute(
        select(FileTagRemoval.entity_id, FileTagRemoval.tag_id).where(
            FileTagRemoval.tag_id.in_(tag_ids)
        )
    )
    return frozenset((e, t) for e, t in rows)


def _restore(conn: Connection, change: TagChange, *, forward: bool) -> None:
    """Put the tag tables into the "after" (redo) or "before" (undo) state and replay or
    reverse the ``entity_tag`` delta, in one transaction."""
    tags = change.tags_after if forward else change.tags_before
    aliases = change.aliases_after if forward else change.aliases_before
    unlink = change.removed if forward else change.added
    relink = change.added if forward else change.removed

    conn.exec_driver_sql("PRAGMA defer_foreign_keys = ON")  # rows may return in any order
    current = {row[0]: row for row in _tag_tables(conn)[0]}
    wanted = {row[0]: row for row in tags}
    gone = set(current) - set(wanted)
    if gone:  # tags the change created: their entity_tag rows cascade
        conn.execute(delete(Tag).where(Tag.id.in_(gone)))
    missing = [_tag_values(wanted[i]) for i in set(wanted) - set(current)]
    if missing:
        conn.execute(insert(Tag), missing)
    for tag_id in set(wanted) & set(current):
        if wanted[tag_id] != current[tag_id]:
            values = _tag_values(wanted[tag_id])
            del values["id"]
            conn.execute(update(Tag).where(Tag.id == tag_id).values(**values))

    conn.execute(delete(TagAlias))
    if aliases:
        conn.execute(insert(TagAlias), [{"tag_id": t, "alias": a} for t, a in aliases])

    if unlink:
        pairs = [(e, t) for e, t, _ in unlink]
        conn.execute(
            delete(EntityTag).where(tuple_(EntityTag.entity_id, EntityTag.tag_id).in_(pairs))
        )
    # File tags (#294) follow the restored tags and removal records; then the user's rows go
    # back, so a file tag removed by hand returns as a file tag, not as the user's.
    keep_off = change.removals_added if forward else change.removals_dropped
    forget = change.removals_dropped if forward else change.removals_added
    if forget:
        forget_removals(conn, forget)
    if keep_off:
        conn.execute(
            insert(FileTagRemoval).prefix_with("OR IGNORE"),
            [{"entity_id": e, "tag_id": t} for e, t in keep_off],
        )
    if change.tags_before != change.tags_after or change.aliases_before != change.aliases_after:
        apply_file_tags(conn)
    else:
        touched = {e for e, _, _ in unlink | relink} | {e for e, _ in keep_off | forget}
        if touched:
            apply_file_tags(conn, touched)
    if relink:
        existing = set(
            conn.scalars(select(Entity.id).where(Entity.id.in_({e for e, _, _ in relink})))
        )
        rows = [
            {"entity_id": e, "tag_id": t, "added_at": a}
            for e, t, a in relink
            if e in existing and t in wanted
        ]
        if rows:
            conn.execute(insert(EntityTag).prefix_with("OR IGNORE"), rows)


def _tag_values(row: TagRow) -> dict[str, Any]:
    tag_id, parent_id, name, color, sort_order, description, types = row
    return {
        "id": tag_id,
        "parent_id": parent_id,
        "name": name,
        "color": color,
        "sort_order": sort_order,
        "description": description,
        "types": types,
    }
