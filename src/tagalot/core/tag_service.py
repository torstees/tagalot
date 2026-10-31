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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, delete, insert, select, tuple_, update

from tagalot.core.actions import ActionChange, restore_action
from tagalot.core.fields import (
    ExtraChange,
    FieldChange,
    FieldEditError,
    NoteEdit,
    edit_extra,
    edit_field,
    edit_note,
    edit_own_field,
    restore_extra,
    restore_field,
    restore_note,
)
from tagalot.core.item_pictures import PictureChange, restore_picture
from tagalot.core.keep_import import (
    ArchiveChange,
    KeepImportPlan,
    TagPhase,
    import_keep,
    restore_archives,
)
from tagalot.core.keywords import (
    IgnoreChange,
    apply_file_tags,
    forget_removals,
    remember_removals,
    restore_ignored,
    set_ignored,
)
from tagalot.core.linked_fields import edit_linked_field
from tagalot.core.merge import MergeChange, restore_merge
from tagalot.core.models import (
    Entity,
    EntityTag,
    FileTagRemoval,
    Tag,
    TagAlias,
    TagGroup,
    TagGroupMember,
)
from tagalot.core.not_duplicates import NotDuplicateChange, restore_not_duplicates
from tagalot.core.note_keys import NoteKeyChange, ignore_note_key, restore_note_key
from tagalot.core.own_fields import OWN, OwnField, OwnFields
from tagalot.core.picture_problems import PictureIgnoreChange, restore_picture_ignores
from tagalot.core.place_links import PlaceChange, restore_place_change
from tagalot.core.promote import FieldMove
from tagalot.core.reextract import ReextractChange, restore_entities
from tagalot.core.saved_searches import SavedChange, restore_saved
from tagalot.core.tag_pictures import set_tag_picture
from tagalot.core.tag_yaml import ImportPlan, import_tags
from tagalot.core.tags import (
    PATH_SEPARATOR,
    DeleteMode,
    PictureData,
    TagTree,
    TagTreeCache,
    add_alias,
    add_group,
    add_tag,
    add_tag_path,
    add_to_group,
    delete_group,
    delete_tag,
    merge_tags,
    remove_alias,
    remove_from_group,
    rename_group,
    rename_tag,
    reparent_tag,
    set_tag_color,
    set_tag_description,
    set_tag_icon,
    set_tag_icon_picture,
    set_tag_types,
    store_icon_picture,
    tag_entities,
    untag_entities,
)
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.triage import DismissChange, restore_dismissals
from tagalot.core.writer import DbWriter
from tagalot.themes.api import FieldInfo

logger = logging.getLogger(__name__)

PictureKey = tuple[str, int]
"""Which picture of which tag: ``("picture", id)`` (#506) or ``("icon", id)`` (#507)."""
PICTURE, ICON = "picture", "icon"


MAX_HISTORY = 100
"""Undo steps kept per session."""

TagRow = tuple[int, int | None, str, str | None, int, str | None, str | None, str | None]
"""``(id, parent_id, name, color, sort_order, description, types, icon)``."""
EntityTagRow = tuple[int, int, datetime]
"""``(entity_id, tag_id, added_at)``."""
GroupRow = tuple[int, str, int]
"""``(id, name, sort_order)`` of a tag group (#534)."""
Groups = tuple[frozenset[GroupRow], frozenset[tuple[int, int]]]
"""The tag groups and their ``(group, tag)`` members, as a step saw them."""


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
    pictures_before: tuple[tuple[PictureKey, PictureData | None], ...] = ()
    """The tags' pictures and icons that are pictures the operation changed, as they were
    (#506, #507): only those, not every tag's, so the history doesn't hold every picture
    for every step."""
    pictures_after: tuple[tuple[PictureKey, PictureData | None], ...] = ()
    """The same afterwards (``None``: none)."""
    groups_before: Groups | None = None
    """The tag groups and their members before (#534); ``None`` in a tagging step, which
    leaves them alone."""
    groups_after: Groups | None = None


LinkDelta = tuple[
    frozenset[EntityTagRow],
    frozenset[EntityTagRow],
    frozenset[tuple[int, int]],
    frozenset[tuple[int, int]],
]
"""What a tagging operation did: the user's rows added and removed, removal records added,
and removal records dropped."""


@dataclass(frozen=True)
class KeepImportChange:
    """Import keep… (#613): its parts' steps, in the order done; one undo step."""

    label: str
    steps: tuple[Any, ...]
    """Each a :data:`Step`."""


Step = (
    TagChange
    | FieldChange
    | ExtraChange
    | NoteEdit
    | NoteKeyChange
    | ReextractChange
    | ActionChange
    | DismissChange
    | IgnoreChange
    | PictureIgnoreChange
    | PictureChange
    | MergeChange
    | PlaceChange
    | NotDuplicateChange
    | SavedChange
    | FieldMove
    | ArchiveChange
    | KeepImportChange
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
        self.last_replayed: Step | None = None
        """The step the last undo or redo replayed."""
        self.on_note_key: Callable[[str, bool], None] | None = None
        """Adds a key to the keep's ignored note keys (``True``) or takes it off, as undo
        and redo replay ignoring it (the session sets it; ``keep.toml`` isn't the DB's)."""
        self.own_fields: Callable[[], OwnFields] = dict
        """The keep's own fields (#581), which :meth:`edit_field` sets as ``own:<name>``
        (the session sets it from ``keep.toml``)."""
        self.on_own_fields: Callable[[OwnFields], None] | None = None
        """Writes the keep's own fields to ``keep.toml`` as a :class:`FieldMove` is undone
        and redone (the session sets it)."""

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

    def set_icon(self, tag_id: int, icon: str | None) -> None:
        """Set or clear a tag's icon (#398); one undoable step."""
        self._record(
            lambda tree: f"Change the icon of {_name(tree, tag_id)!r}",
            lambda tree: (),
            lambda conn: set_tag_icon(conn, tag_id, icon),
        )

    def set_icon_picture(self, tag_id: int, picture: PictureData | None) -> None:
        """Set a picture as a tag's icon, or clear it (#507); one undoable step."""
        self._record(
            lambda tree: f"Change the icon of {_name(tree, tag_id)!r}",
            lambda tree: (),
            lambda conn: set_tag_icon_picture(conn, tag_id, picture),
        )

    def set_picture(self, tag_id: int, picture: PictureData | None) -> None:
        """Set or clear a tag's picture (#506); one undoable step."""
        self._record(
            lambda tree: f"Change the picture of {_name(tree, tag_id)!r}",
            lambda tree: (),
            lambda conn: set_tag_picture(conn, tag_id, picture),
        )

    def set_description(self, tag_id: int, description: str | None) -> None:
        self._record(
            lambda tree: f"Change the description of {_name(tree, tag_id)!r}",
            lambda tree: (),
            lambda conn: set_tag_description(conn, tag_id, description),
        )

    # --- tag groups (#534) ---

    def add_group(self, name: str, tag_ids: Iterable[int] = ()) -> int:
        """Create a tag group, holding ``tag_ids``; one undoable step."""
        tags = frozenset(tag_ids)
        return self._record(
            lambda tree: f"Add group {' '.join(name.split())!r}",
            lambda tree: (),
            lambda conn: add_group(conn, name, tags),
        )

    def rename_group(self, group_id: int, name: str) -> None:
        self._record(
            lambda tree: f"Rename group {_group(tree, group_id)!r} to {' '.join(name.split())!r}",
            lambda tree: (),
            lambda conn: rename_group(conn, group_id, name),
        )

    def delete_group(self, group_id: int) -> None:
        """Delete a tag group (never its tags); one undoable step."""
        self._record(
            lambda tree: f"Delete group {_group(tree, group_id)!r}",
            lambda tree: (),
            lambda conn: delete_group(conn, group_id),
        )

    def add_to_group(self, group_id: int, tag_ids: Iterable[int]) -> int:
        """Put tags in a group; one undoable step. Returns how many were added."""
        tags = frozenset(tag_ids)
        return self._record(
            lambda tree: f"Add {_names(tree, tags)} to group {_group(tree, group_id)!r}",
            lambda tree: (),
            lambda conn: add_to_group(conn, group_id, tags),
        )

    def remove_from_group(self, group_id: int, tag_ids: Iterable[int]) -> int:
        """Take tags out of a group; one undoable step. Returns how many were taken out."""
        tags = frozenset(tag_ids)
        return self._record(
            lambda tree: f"Take {_names(tree, tags)} out of group {_group(tree, group_id)!r}",
            lambda tree: (),
            lambda conn: remove_from_group(conn, group_id, tags),
        )

    def import_tags(
        self,
        text: str,
        name: str,
        *,
        delete_unused: bool = False,
        match_items: bool = False,
        base: Path | None = None,
    ) -> ImportPlan:
        """Import a YAML tag file (``name`` is how the step reads in Undo) as one undoable
        step; returns what it did (#395). Changing a tag's ``applies_to`` can take it off
        items, and the file can tag items (#396), so every tag's uses are in the step's
        scope."""
        schema = self.schema
        assert schema is not None
        return self._record(
            lambda tree: f"Import tags from {name!r}",
            lambda tree: tree,
            lambda conn: import_tags(conn, text, schema, delete_unused, match_items, base),
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

    def restore_file_tag(self, entity_id: int, tag_id: int) -> int:
        """Put back a file tag the user removed from an item: forget the removal, so its
        keyword gives the tag again (#295). One undo step; returns 1 if it changed."""

        def op(conn: Connection) -> LinkDelta:
            dropped = forget_removals(conn, {(entity_id, tag_id)})
            if dropped:
                apply_file_tags(conn, [entity_id])
            return frozenset(), frozenset(), frozenset(), dropped

        return self._record_links(
            lambda tree: f"Restore {_name(tree, tag_id)!r} from the item's file",
            op,
            count_removals=True,
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

    def edit_field(self, entity_id: int, name: str, value: Any) -> FieldChange | ActionChange:
        """Set a field (or ``"title"``) by hand: provenance ``user``, one undo step. A field
        that mirrors a link changes the links too (#440). Raises
        :class:`~tagalot.core.fields.FieldEditError` for a value that doesn't fit."""
        schema = self.schema
        if schema is None:
            raise RuntimeError("field edits need the keep's schema")
        theme = schema.theme()

        own_fields = self.own_fields()

        def edit(conn: Connection) -> FieldChange | ActionChange:
            if name.startswith(OWN):  # one of the keep's own fields (#581)
                own = _own_field(conn, own_fields, entity_id, name)
                return edit_own_field(conn, schema, own, entity_id, value)
            mirrored = _mirrored(conn, schema, entity_id, name)
            if mirrored is not None:
                return edit_linked_field(conn, schema, theme, entity_id, mirrored, value)
            return edit_field(conn, schema, entity_id, name, value)

        done = self.writer.run(edit)
        if isinstance(done, ActionChange):
            if done.before != done.after:
                self._push(done)
            return done
        change = done
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

    def edit_note(self, entity_id: int, body: str | None) -> NoteEdit:
        """Set the note typed in Tagalot for an item (#383); one undo step. Raises
        :class:`~tagalot.core.fields.FieldEditError` when the item's note is a file."""
        schema = self.schema
        if schema is None:
            raise RuntimeError("notes need the keep's schema")
        change = self.writer.run(lambda conn: edit_note(conn, schema, entity_id, body))
        if change.before != change.after:
            self._push(change)
        return change

    def ignore_note_key(self, key: str, *, listed: bool = False) -> NoteKeyChange:
        """Remove the Extra fields notes gave under ``key``, in every item; one undo step.
        The caller lists the key in ``keep.toml`` (``listed``: it was there already)."""
        schema = self.schema
        if schema is None:
            raise RuntimeError("note keys need the keep's schema")
        change = self.writer.run(lambda conn: ignore_note_key(conn, schema, key, listed=listed))
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

    def import_keep(
        self,
        data: Mapping[str, Any],
        name: str,
        *,
        base: Path | None = None,
        replace: bool = False,
        preview: bool = False,
    ) -> KeepImportPlan:
        """Load a keep export (``data``, from :func:`~tagalot.core.keep_import.read_export`)
        as one undo step (#613); with ``preview``, only say what it would do. ``replace``
        settles conflicts with the file's values."""
        schema = self.schema
        if schema is None:
            raise RuntimeError("a keep import needs the keep's schema")
        own = self.own_fields()
        label = f"Import keep export {name!r}"

        def tag_phase(
            conn: Connection, op: Callable[[Connection], ImportPlan]
        ) -> tuple[ImportPlan, TagChange]:
            return self._tag_job(lambda tree: label, lambda tree: tree, op)(conn)

        phase: TagPhase = tag_phase

        def job(conn: Connection) -> tuple[KeepImportPlan, list[Any]]:
            return import_keep(
                conn, schema, data, own_fields=own, tag_phase=phase, base=base, replace=replace
            )

        if preview:
            return self.writer.preview(job)[0]
        plan, steps = self._apply(job)
        if plan.changes:
            self._push(KeepImportChange(label, tuple(steps)))
        return plan

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
        self.last_replayed = change
        self._redo.append(change)
        return change.label

    def redo(self) -> str | None:
        """Redo the most recently undone operation; returns its label, or ``None``."""
        if not self._redo:
            return None
        change = self._redo.pop()
        self._replay(change, forward=True)
        self.last_replayed = change
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
        elif isinstance(step, NoteEdit):
            assert schema is not None
            self.writer.run(lambda conn: restore_note(conn, schema, step, forward=forward))
        elif isinstance(step, NoteKeyChange):
            assert schema is not None
            self.writer.run(lambda conn: restore_note_key(conn, schema, step, forward=forward))
            if self.on_note_key is not None and not step.listed:
                self.on_note_key(step.key, forward)
        elif isinstance(step, ReextractChange):
            assert schema is not None
            self.writer.run(lambda conn: restore_entities(conn, schema, step, forward=forward))
        elif isinstance(step, DismissChange):
            self.writer.run(lambda conn: restore_dismissals(conn, step, forward=forward))
        elif isinstance(step, IgnoreChange):
            self.writer.run(lambda conn: restore_ignored(conn, step, forward=forward))
        elif isinstance(step, PictureChange):
            self.writer.run(lambda conn: restore_picture(conn, step, forward=forward))
        elif isinstance(step, PictureIgnoreChange):
            self.writer.run(lambda conn: restore_picture_ignores(conn, step, forward=forward))
        elif isinstance(step, ActionChange):
            assert schema is not None
            self.writer.run(lambda conn: restore_action(conn, schema, step, forward=forward))
        elif isinstance(step, SavedChange):
            self.writer.run(lambda conn: restore_saved(conn, step, forward=forward))
        elif isinstance(step, NotDuplicateChange):
            self.writer.run(lambda conn: restore_not_duplicates(conn, step, forward=forward))
        elif isinstance(step, FieldMove):
            assert schema is not None
            action = step.action
            self.writer.run(lambda conn: restore_action(conn, schema, action, forward=forward))
            if self.on_own_fields is not None:
                self.on_own_fields(step.fields_after if forward else step.fields_before)
        elif isinstance(step, MergeChange):
            assert schema is not None
            self.writer.run(lambda conn: restore_merge(conn, schema, step, forward=forward))
        elif isinstance(step, PlaceChange):
            assert schema is not None
            self.writer.run(lambda conn: restore_place_change(conn, schema, step, forward=forward))
        elif isinstance(step, ArchiveChange):
            self.writer.run(lambda conn: restore_archives(conn, step, forward=forward))
        elif isinstance(step, KeepImportChange):
            for part in step.steps if forward else reversed(step.steps):
                self._replay(part, forward=forward)
        else:
            self._apply(lambda conn: _restore(conn, step, forward=forward))

    def _record[T](
        self,
        label: Callable[[TagTree], str],
        scope: Callable[[TagTree], Iterable[int]],
        op: Callable[[Connection], T],
    ) -> T:
        result, change = self._apply(self._tag_job(label, scope, op))
        self._push(change)
        return result

    def _tag_job[T](
        self,
        label: Callable[[TagTree], str],
        scope: Callable[[TagTree], Iterable[int]],
        op: Callable[[Connection], T],
    ) -> Callable[[Connection], tuple[T, TagChange]]:
        """A writer job running ``op`` and recording it as a :class:`TagChange`."""

        def job(conn: Connection) -> tuple[T, TagChange]:
            tree = TagTree.load(conn)
            tag_ids = frozenset(scope(tree))
            tags_before, aliases_before = _tag_tables(conn)
            links_before = _links(conn, tag_ids)
            removals_before = _removals(conn, tag_ids)
            pictures_before = _pictures(conn)
            groups_before = _groups(conn)
            result = op(conn)
            apply_file_tags(conn)  # a rename, alias, or type change changes what matches
            tags_after, aliases_after = _tag_tables(conn)
            # Tags the operation made can have uses already (an import tagging items, #396).
            made = frozenset(r[0] for r in tags_after) - frozenset(r[0] for r in tags_before)
            links_after = _links(conn, tag_ids | made)
            removals_after = _removals(conn, tag_ids)
            pictures_after = _pictures(conn)
            changed = sorted(
                t
                for t in pictures_before.keys() | pictures_after.keys()
                if pictures_before.get(t) != pictures_after.get(t)
            )
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
                pictures_before=tuple((t, pictures_before.get(t)) for t in changed),
                pictures_after=tuple((t, pictures_after.get(t)) for t in changed),
                groups_before=groups_before,
                groups_after=_groups(conn),
            )
            return result, change

        return job

    def _record_links(
        self,
        label: Callable[[TagTree], str],
        op: Callable[[Connection], "LinkDelta"],
        *,
        count_removals: bool = False,
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
        removals = len(change.removals_added) + len(change.removals_dropped)
        if change.added or change.removed or (count_removals and removals):
            self._push(change)
        return len(change.added) + len(change.removed) + (removals if count_removals else 0)

    def _apply[T](self, job: Callable[[Connection], T]) -> T:
        try:
            return self.writer.run(job)
        finally:
            self.cache.invalidate()


def _name(tree: TagTree, tag_id: int) -> str:
    return tree.node(tag_id).name if tag_id in tree else f"#{tag_id}"


def _group(tree: TagTree, group_id: int) -> str:
    try:
        return tree.group(group_id).name
    except KeyError:
        return f"#{group_id}"


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
            Tag.id,
            Tag.parent_id,
            Tag.name,
            Tag.color,
            Tag.sort_order,
            Tag.description,
            Tag.types,
            Tag.icon,
        )
    )
    aliases = conn.execute(select(TagAlias.tag_id, TagAlias.alias))
    return frozenset(tuple(r) for r in tags), frozenset(tuple(r) for r in aliases)  # type: ignore[misc]


def _groups(conn: Connection) -> Groups:
    """Every tag group and member now (#534): few and small, like the tag tables."""
    groups = conn.execute(select(TagGroup.id, TagGroup.name, TagGroup.sort_order))
    members = conn.execute(select(TagGroupMember.group_id, TagGroupMember.tag_id))
    return (
        frozenset((i, n, o) for i, n, o in groups),
        frozenset((g, t) for g, t in members),
    )


def _restore_groups(conn: Connection, groups: Groups, tag_ids: Iterable[int]) -> None:
    """Put the tag groups and their members back as they were (members of tags that are
    there)."""
    rows, members = groups
    conn.execute(delete(TagGroupMember))
    conn.execute(delete(TagGroup))
    if rows:
        conn.execute(insert(TagGroup), [{"id": i, "name": n, "sort_order": o} for i, n, o in rows])
    tags = set(tag_ids)
    kept = [{"group_id": g, "tag_id": t} for g, t in members if t in tags]
    if kept:
        conn.execute(insert(TagGroupMember), kept)


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
    groups = change.groups_after if forward else change.groups_before
    if groups is not None and change.groups_before != change.groups_after:
        _restore_groups(conn, groups, wanted)

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
    # Tags' pictures and icons that are pictures, after their tags are back (#506, #507).
    for (kind, tag_id), picture in change.pictures_after if forward else change.pictures_before:
        if tag_id in wanted and kind == ICON:
            store_icon_picture(conn, tag_id, picture)
        elif tag_id in wanted:
            set_tag_picture(conn, tag_id, picture)
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


def _pictures(conn: Connection) -> dict[PictureKey, PictureData]:
    """Every tag's picture and icon that is a picture now: read around an operation to keep
    only those it changed."""
    tree = TagTree.load(conn)
    found: dict[PictureKey, PictureData] = {(PICTURE, t): p for t, p in tree.pictures().items()}
    found |= {(ICON, t): p for t, p in tree.icon_pictures().items()}
    return found


def _tag_values(row: TagRow) -> dict[str, Any]:
    tag_id, parent_id, name, color, sort_order, description, types, icon = row
    return {
        "icon": icon,
        "id": tag_id,
        "parent_id": parent_id,
        "name": name,
        "color": color,
        "sort_order": sort_order,
        "description": description,
        "types": types,
    }


def _own_field(conn: Connection, fields: OwnFields, entity_id: int, key: str) -> OwnField:
    """The keep's own field ``own:<name>`` of the item's type."""
    type_id = conn.scalar(select(Entity.type).where(Entity.id == entity_id))
    if type_id is None:
        raise FieldEditError("This item no longer exists")
    own = fields.get(type_id, {}).get(key.removeprefix(OWN))
    if own is None:
        raise FieldEditError("That field was removed in Keep configuration")
    return own


def _mirrored(conn: Connection, schema: ThemeSchema, entity_id: int, name: str) -> FieldInfo | None:
    """The field ``name`` of the item, when it mirrors a link (#440)."""
    type_id = conn.scalar(select(Entity.type).where(Entity.id == entity_id))
    if type_id is None:
        return None
    try:
        table = schema.by_type_id(type_id)
    except KeyError:
        return None
    info = next((f for f in table.fields if f.name == name), None)
    return info if info is not None and info.spec.mirrors is not None else None
