"""Triage lists: things that need attention (DESIGN.md §12 "Triage").

- **Unlinked files** (:data:`UNLINKED`): files no item uses (folders aren't listed, and
  neither are files known to be missing). :func:`unlinked_files` lists them.
- **Untagged items** (:data:`UNTAGGED`) and **items whose files are all missing**
  (:data:`MISSING`): searches with ``SearchSpec.triage`` (see :func:`triage_condition`), so
  their pages are ordinary search pages.

**Dismissing** hides an unlinked file or an untagged item until it changes: the dismissal
stores a *marker* of how it looked (a file's size and time, an item's ``updated_at``, as
stored), and the lists leave out only rows whose marker still matches. It is one undo step
(:class:`DismissChange`).

**Deleting** items whose files are all missing is ``core.actions.delete_items``: one undo
step, through the same whole-entity snapshots theme actions use.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import (
    ColumnElement,
    Connection,
    String,
    and_,
    cast,
    delete,
    func,
    insert,
    or_,
    select,
    type_coerce,
)
from sqlalchemy.orm import InstrumentedAttribute, aliased

from tagalot.core.keywords import keyword_index
from tagalot.core.member_paths import shown_path
from tagalot.core.models import (
    Entity,
    EntityAncestor,
    EntityKeyword,
    EntityNote,
    EntityResource,
    EntityTag,
    KeywordIgnored,
    LinkedPlace,
    NotesFolderNote,
    Resource,
    ResourceKind,
    ResourceStatus,
    Root,
    TriageDismissal,
)
from tagalot.core.tags import TagTree

UNLINKED, UNTAGGED, MISSING = "unlinked", "untagged", "missing"
KEYWORDS = "keywords"
"""Items with a file keyword that matches no tag and isn't ignored (#295)."""
ENTITY_LISTS = (UNTAGGED, MISSING, KEYWORDS)
"""The lists that are searches (``SearchSpec.triage``)."""
DISMISSABLE = (UNLINKED, UNTAGGED)


# --- markers: how a dismissed thing looked ---


def resource_marker() -> ColumnElement[str]:
    """A file's size and modification time, as text."""
    return (
        func.coalesce(cast(Resource.size, String), "")
        + ":"
        + func.coalesce(cast(Resource.mtime_ns, String), "")
    )


def entity_marker() -> ColumnElement[str]:
    """An item's ``updated_at`` as stored (edits and re-reads change it)."""
    return type_coerce(Entity.updated_at, String)


def _not_dismissed(
    name: str, target: InstrumentedAttribute[int], marker: ColumnElement[str]
) -> ColumnElement[bool]:
    return ~(
        select(TriageDismissal.target_id)
        .where(
            TriageDismissal.list == name,
            TriageDismissal.target_id == target,
            TriageDismissal.marker == marker,
        )
        .exists()
    )


# --- the entity lists, as search conditions ---


def triage_condition(
    name: str, inherit_tags: bool = False, tree: TagTree | None = None
) -> ColumnElement[bool]:
    """Which entities a triage list holds (``SearchSpec.triage``). The keywords list needs
    the tag tree (what keywords match)."""
    if name == KEYWORDS:
        matched = list(keyword_index(tree or TagTree([], {})))
        unmatched = select(EntityKeyword.entity_id).where(
            EntityKeyword.match_key.not_in(matched),
            EntityKeyword.match_key.not_in(select(KeywordIgnored.match_key)),
        )
        return Entity.id.in_(unmatched)
    if name == UNTAGGED:
        if inherit_tags:  # a tag on a container counts, as in searches (§8)
            tagged = select(EntityAncestor.entity_id).join(
                EntityTag, EntityTag.entity_id == EntityAncestor.ancestor_id
            )
        else:
            tagged = select(EntityTag.entity_id)
        return and_(
            Entity.id.not_in(tagged),
            _not_dismissed(UNTAGGED, Entity.id, entity_marker()),
        )
    if name == MISSING:
        present = (
            select(EntityResource.entity_id)
            .join(Resource, Resource.id == EntityResource.resource_id)
            .where(or_(Resource.status != ResourceStatus.MISSING, Resource.skipped))
        )
        elsewhere = (  # a file is there while any of its linked places is (#598)
            select(EntityResource.entity_id)
            .join(LinkedPlace, LinkedPlace.first_id == EntityResource.resource_id)
            .join(Resource, Resource.id == LinkedPlace.resource_id)
            .where(Resource.status != ResourceStatus.MISSING)
        )
        return and_(
            Entity.id.in_(select(EntityResource.entity_id)),
            Entity.id.not_in(present),
            Entity.id.not_in(elsewhere),
        )
    raise ValueError(f"no triage list {name!r}")


# --- unlinked files ---


@dataclass(frozen=True)
class UnlinkedFile:
    resource_id: int
    root_id: str
    root_name: str
    relpath: str
    size: int | None
    status: ResourceStatus
    archive: str | None = None
    """For a file inside an archive: the archive's relpath (#524)."""

    @property
    def shown(self) -> str:
        """Its path as shown (a member's: its archive's, then its path inside it)."""
        return shown_path(self.relpath, self.archive)


NotesFolder = tuple[str, str] | None
"""The keep's notes folder: its root id and path (``""``: the whole root), if it has one."""


def _outside_notes(notes: NotesFolder) -> list[ColumnElement[bool]]:
    """Not in the notes folder, whose files aren't content (#382, #388): its notes are
    listed under Notes, and its pictures are galleries' or nothing."""
    if notes is None:
        return []
    root_id, path = notes
    if not path:
        return [Resource.root_id != root_id]
    folder = path.casefold()
    inside = func.lower(Resource.relpath).startswith(folder + "/", autoescape=True)
    return [~and_(Resource.root_id == root_id, inside | (func.lower(Resource.relpath) == folder))]


def _unlinked_where(notes: NotesFolder = None) -> list[ColumnElement[bool]]:
    return [
        *_outside_notes(notes),
        Resource.kind == ResourceKind.FILE,
        Resource.status != ResourceStatus.MISSING,
        Resource.skipped.is_(False),
        Resource.id.not_in(select(EntityResource.resource_id)),
        Resource.id.not_in(select(LinkedPlace.resource_id)),  # another place of a file (#598)
        Resource.id.not_in(  # an item's note is used, though not linked (#379)
            select(EntityNote.resource_id).where(EntityNote.resource_id.is_not(None))
        ),
        Resource.id.not_in(select(NotesFolderNote.resource_id)),  # listed under Notes
        _not_dismissed(UNLINKED, Resource.id, resource_marker()),
    ]


def unlinked_files(
    conn: Connection, limit: int | None = None, notes: NotesFolder = None
) -> list[UnlinkedFile]:
    """Files no item uses, by folder and path; not those in the notes folder."""
    archive = aliased(Resource)
    query = (
        select(
            Resource.id,
            Resource.root_id,
            Root.name,
            Resource.relpath,
            Resource.size,
            Resource.status,
            archive.relpath,
        )
        .join(Root, Root.id == Resource.root_id)
        .outerjoin(archive, archive.id == Resource.parent_resource_id)
        .where(*_unlinked_where(notes))
        .order_by(Root.name, Resource.relpath)
        .limit(limit)
    )
    return [UnlinkedFile(*row) for row in conn.execute(query)]


def count_unlinked(conn: Connection, notes: NotesFolder = None) -> int:
    query = select(func.count()).select_from(Resource).where(*_unlinked_where(notes))
    return int(conn.scalar(query) or 0)


# --- the notes folder's notes (#382) ---


@dataclass(frozen=True)
class NoteProblem:
    """A notes-folder note that isn't an item's note: it matches no item, several, or an
    item that has another note."""

    resource_id: int
    root_id: str
    relpath: str
    status: ResourceStatus
    matches: int
    item_title: str | None
    """The item it matched, when it matched one (which has another note)."""

    @property
    def reason(self) -> str:
        if self.matches == 0:
            return "No item has this title or one of its aliases"
        if self.matches > 1:
            return f"{self.matches} items match; add tagalot-type, or an alias only one of them has"
        return f"{self.item_title!r} has another note"


def _note_problem_where() -> list[ColumnElement[bool]]:
    return [
        Resource.status != ResourceStatus.MISSING,
        or_(NotesFolderNote.matches != 1, NotesFolderNote.applied.is_(False)),
    ]


def note_problems(conn: Connection) -> list[NoteProblem]:
    """The notes folder's notes that aren't items' notes, by path."""
    query = (
        select(
            Resource.id,
            Resource.root_id,
            Resource.relpath,
            Resource.status,
            NotesFolderNote.matches,
            Entity.title,
        )
        .join(Resource, Resource.id == NotesFolderNote.resource_id)
        .join(Entity, Entity.id == NotesFolderNote.item_id, isouter=True)
        .where(*_note_problem_where())
        .order_by(Resource.relpath)
    )
    return [NoteProblem(*row) for row in conn.execute(query)]


def count_note_problems(conn: Connection) -> int:
    query = (
        select(func.count())
        .select_from(NotesFolderNote)
        .join(Resource, Resource.id == NotesFolderNote.resource_id)
        .where(*_note_problem_where())
    )
    return int(conn.scalar(query) or 0)


def exact_pattern(relpath: str) -> str:
    """An exclude pattern (§4) matching just this path: glob characters are bracketed,
    and so is ``#``, which starts a note in the Skip box (#334)."""
    return "".join(f"[{c}]" if c in "*?[#" else c for c in relpath)


# --- dismissing ---


@dataclass(frozen=True)
class DismissChange:
    """Everything needed to undo and redo a dismissal (an undo step)."""

    label: str
    list: str
    before: frozenset[tuple[int, str]]
    """(target id, marker) rows for these targets before."""
    after: frozenset[tuple[int, str]]


def dismiss(conn: Connection, name: str, ids: Sequence[int]) -> DismissChange:
    """Hide these files or items from a triage list until they change."""
    if name not in DISMISSABLE:
        raise ValueError(f"the {name!r} list can't be dismissed from")
    before = _rows(conn, name, ids)
    conn.execute(
        delete(TriageDismissal).where(
            TriageDismissal.list == name, TriageDismissal.target_id.in_(ids)
        )
    )
    source = (
        select(Resource.id, resource_marker()).where(Resource.id.in_(ids))
        if name == UNLINKED
        else select(Entity.id, entity_marker()).where(Entity.id.in_(ids))
    )
    rows = [{"list": name, "target_id": i, "marker": m} for i, m in conn.execute(source)]
    if rows:
        conn.execute(insert(TriageDismissal), rows)
    what = "file" if name == UNLINKED else "item"
    label = f"Dismiss {len(rows)} {what}{'' if len(rows) == 1 else 's'}"
    return DismissChange(label, name, before, _rows(conn, name, ids))


def restore_dismissals(conn: Connection, change: DismissChange, *, forward: bool) -> None:
    """Undo (``forward=False``) or redo a dismissal."""
    ids = [i for i, _ in change.before | change.after]
    conn.execute(
        delete(TriageDismissal).where(
            TriageDismissal.list == change.list, TriageDismissal.target_id.in_(ids)
        )
    )
    rows = change.after if forward else change.before
    if rows:
        conn.execute(
            insert(TriageDismissal),
            [{"list": change.list, "target_id": i, "marker": m} for i, m in rows],
        )


def _rows(conn: Connection, name: str, ids: Sequence[int]) -> frozenset[tuple[int, str]]:
    return frozenset(
        (i, m)
        for i, m in conn.execute(
            select(TriageDismissal.target_id, TriageDismissal.marker).where(
                TriageDismissal.list == name, TriageDismissal.target_id.in_(ids)
            )
        )
    )
