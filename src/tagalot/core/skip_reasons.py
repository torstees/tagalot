"""Why files are skipped, for Triage's Skipped tab (#527, DESIGN.md §12 *Triage*).

A skipped file (``resource.skipped``, §6) is one a scan now leaves out; its item is hidden
while all its files are (#526). The reason isn't stored: each skipped file is checked
against the rules in force now, as a scan would, so the list always says what to change to
read it again. A file can be under more than one rule (two Skip patterns); it is listed
under each, and counted once in the total.

The rules, in the order they're listed:

- each of a folder's **Skip patterns** (itself or a folder above it matching);
- the **notes folder**, which records only notes, pictures, and archives;
- **folders** the theme doesn't record, and each **extension** it doesn't read;
- inside archives: members of a **skipped archive**, of an archive **turned off**,
  **hidden** members of one read member by member, and members of archives **not read**
  (no ``archives`` pattern or choice for them, §6 *Archive members*);
- files no rule leaves out any more: read again at the next scan.
"""

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from re import Pattern

from sqlalchemy import Connection, select
from sqlalchemy.orm import aliased

from tagalot.core.archive_members import (
    ArchiveState,
    archive_patterns,
    archive_states,
    member_choices,
)
from tagalot.core.keep import RootConfig
from tagalot.core.member_paths import member_name, shown_path
from tagalot.core.models import ArchiveMode, EntityResource, Resource, ResourceKind
from tagalot.core.scanner import (
    NOTES_FOLDER_EXTENSIONS,
    DirRule,
    compile_excludes,
    in_notes,
    is_folder_note,
)
from tagalot.core.skipped_items import skipped_items

MAX_LISTED = 10_000
"""Files listed per rule (its count says how many there are)."""

PATTERN = "pattern"
NOTES = "notes"
FOLDERS = "folders"
EXTENSION = "extension"
ARCHIVE_SKIPPED = "archive skipped"
ARCHIVE_OFF = "archive off"
MEMBER_HIDDEN = "member hidden"
NOT_READ_INSIDE = "not read inside"
NEXT_SCAN = "next scan"
_ORDER = (
    PATTERN,
    NOTES,
    FOLDERS,
    EXTENSION,
    ARCHIVE_SKIPPED,
    ARCHIVE_OFF,
    MEMBER_HIDDEN,
    NOT_READ_INSIDE,
    NEXT_SCAN,
)


@dataclass(frozen=True)
class SkippedFile:
    resource_id: int
    root_id: str
    relpath: str
    archive: str = ""
    """For a file inside an archive: the archive's relpath (#524)."""

    @property
    def shown(self) -> str:
        """Its path as shown (a member's: its archive's, then its path inside it)."""
        return shown_path(self.relpath, self.archive)


@dataclass
class SkipRule:
    """One rule that skips files now, with the files it skips."""

    kind: str
    title: str
    """What the rule is: ``Skip pattern "Drafts/**"``, ``The theme doesn't read .bmp``."""
    where: str
    """Where it lives, and how to read the files again."""
    root_name: str = ""
    """The folder it belongs to; empty for the theme's rules, which apply to every folder."""
    value: str = ""
    """The pattern or the extension."""
    count: int = 0
    hidden: int = 0
    """Items hidden because of it: items whose files are all skipped, one of them here."""
    files: list[SkippedFile] = field(default_factory=list)
    """The first :data:`MAX_LISTED` of its files, by path."""


@dataclass(frozen=True)
class SkippedReport:
    rules: list[SkipRule]
    total: int
    """Skipped files, each counted once."""


@dataclass(frozen=True)
class ThemeScope:
    """What the keep's theme reads, as a scan uses it."""

    extensions: Collection[str] | None
    """Extensions it reads (``None``: every file)."""
    dirs: DirRule = False
    see_through: frozenset[str] = frozenset()


def skip_reasons(
    conn: Connection,
    roots: Sequence[RootConfig],
    theme: ThemeScope,
    notes: tuple[str, str] | None = None,
) -> SkippedReport:
    """Every rule that skips at least one file now, with its files and counts.
    ``notes`` is the notes folder's (root id, path), if the keep has one."""
    archive = aliased(Resource)
    rows = conn.execute(
        select(
            Resource.id,
            Resource.root_id,
            Resource.relpath,
            Resource.kind,
            Resource.ext,
            Resource.parent_resource_id,
            archive.relpath,
        )
        .outerjoin(archive, archive.id == Resource.parent_resource_id)
        .where(Resource.skipped.is_(True))
        .order_by(Resource.root_id, Resource.relpath)
    ).all()
    if not rows:
        return SkippedReport([], 0)
    wanted = {e.lower() for e in theme.extensions} if theme.extensions is not None else None
    rules: dict[tuple[str, ...], SkipRule] = {}
    by_root = {r.id: r for r in roots}
    members: dict[str, Callable[[int, str], tuple[str, ...]]] = {}
    patterns = {r.id: _patterns(r.exclude) for r in roots}
    files_of: dict[tuple[str, ...], list[int]] = {}
    for rid, root_id, relpath, kind, ext, parent, outer in rows:
        root = by_root.get(root_id)
        if root is None:
            continue  # a folder no longer in the keep: not this list's business
        file = SkippedFile(rid, root_id, relpath, outer or "")
        if parent is not None:
            if root_id not in members:
                members[root_id] = _member_rules(conn, root)
            keys = [members[root_id](parent, relpath)]
        else:
            notes_path = notes[1] if notes is not None and notes[0] == root_id else None
            keys = _file_rules(
                root, patterns[root_id], relpath, kind, ext, wanted, theme, notes_path
            )
        for key in keys:
            rule = rules.get(key)
            if rule is None:
                rule = rules[key] = _describe(key, root)
            rule.count += 1
            if len(rule.files) < MAX_LISTED:
                rule.files.append(file)
            files_of.setdefault(key, []).append(rid)

    hidden = set(conn.scalars(skipped_items()))
    items_of = _items_of(conn, [r.id for r in rows])
    for key, rule in rules.items():
        rule.hidden = len(
            {e for rid in files_of[key] for e in items_of.get(rid, ()) if e in hidden}
        )
    ordered = sorted(
        rules.items(), key=lambda kv: (_ORDER.index(kv[0][0]), kv[1].root_name.casefold(), kv[0])
    )
    return SkippedReport([rule for _, rule in ordered], len({r.id for r in rows}))


def _patterns(exclude: Iterable[str]) -> list[tuple[str, Pattern[str]]]:
    """A folder's Skip patterns, each compiled on its own."""
    found = []
    for pattern in exclude:
        compiled = compile_excludes([pattern])
        if compiled is not None:
            found.append((pattern, compiled))
    return found


def _file_rules(
    root: RootConfig,
    patterns: list[tuple[str, Pattern[str]]],
    relpath: str,
    kind: ResourceKind,
    ext: str,
    wanted: set[str] | None,
    theme: ThemeScope,
    notes: str | None,
) -> list[tuple[str, ...]]:
    """The rules that leave a file (not inside an archive) out, as ``walk_scope`` decides."""
    parts = relpath.split("/")
    prefixes = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
    keys: list[tuple[str, ...]] = []
    for pattern, compiled in patterns:
        if any(compiled.match(p) for p in prefixes):
            keys.append((PATTERN, root.id, pattern))
    if keys:
        return keys
    if in_notes(relpath, notes):
        if kind is ResourceKind.FILE and ext.lower() in NOTES_FOLDER_EXTENSIONS:
            return [(NEXT_SCAN, root.id)]
        return [(NOTES, root.id)]
    if kind is ResourceKind.DIR:
        dirs = theme.dirs
        if dirs is True or (callable(dirs) and dirs(relpath)):
            return [(NEXT_SCAN, root.id)]
        return [(FOLDERS,)]
    if (
        wanted is None
        or ext.lower() in wanted
        or is_folder_note(relpath, theme.dirs, theme.see_through)
    ):
        return [(NEXT_SCAN, root.id)]
    return [(EXTENSION, ext.lower())]


def _member_rules(conn: Connection, root: RootConfig) -> Callable[[int, str], tuple[str, ...]]:
    """For a root's archive members: the rule that leaves one out."""
    states = {a.id: a for a in archive_states(conn, root.id)}
    patterns = archive_patterns(root.archives)
    choices: dict[int, dict[str, bool | None]] = {}

    def rule(archive_id: int, relpath: str) -> tuple[str, ...]:
        archive: ArchiveState | None = states.get(archive_id)
        if archive is None:
            return (NEXT_SCAN, root.id)
        if archive.skipped:
            return (ARCHIVE_SKIPPED, root.id)
        mode = archive.effective(patterns)
        if mode is ArchiveMode.NONE:
            return (ARCHIVE_OFF if archive.mode is ArchiveMode.NONE else NOT_READ_INSIDE, root.id)
        if archive_id not in choices:
            choices[archive_id] = member_choices(conn, archive_id)
        read = choices[archive_id].get(member_name(archive.relpath, relpath))
        # Member by member, only those chosen are read; read whole, all but those hidden.
        if read is False or (mode is ArchiveMode.CHOSEN and not read):
            return (MEMBER_HIDDEN, root.id)
        return (NEXT_SCAN, root.id)

    return rule


def _describe(key: tuple[str, ...], root: RootConfig) -> SkipRule:
    kind = key[0]
    if kind == PATTERN:
        pattern = key[2]
        return SkipRule(
            kind,
            f"Skip pattern “{pattern}”",
            f"In the Skip box of {root.name} (Keep configuration → Folders). Remove the "
            "pattern to read these files again.",
            root.name,
            pattern,
        )
    if kind == NOTES:
        return SkipRule(
            kind,
            "Not a note in the notes folder",
            "The notes folder records only notes, pictures, and archives.",
            root.name,
        )
    if kind == FOLDERS:
        return SkipRule(
            kind,
            "Folders the theme doesn't record",
            "The keep's theme doesn't make items of these folders. Another theme, or a "
            "newer version of it, may.",
        )
    if kind == EXTENSION:
        ext = key[1]
        what = ext if ext else "files without an extension"
        return SkipRule(
            kind,
            f"The theme doesn't read {what}",
            "The keep's theme doesn't read these files. Another theme, or a newer version "
            "of it, may.",
            value=ext,
        )
    if kind == ARCHIVE_SKIPPED:
        return SkipRule(
            kind,
            "Inside skipped archives",
            "Their archives are skipped, so nothing inside them is read. Read the archive "
            "again to read these.",
            root.name,
        )
    if kind == ARCHIVE_OFF:
        return SkipRule(
            kind,
            "Archives turned off",
            "These archives were set to read nothing inside them.",
            root.name,
        )
    if kind == MEMBER_HIDDEN:
        return SkipRule(
            kind,
            "Hidden inside archives",
            "These files were hidden inside their archives. Right-click an archive, then "
            "Choose what to read inside… to show them again, or Read everything inside.",
            root.name,
        )
    if kind == NOT_READ_INSIDE:
        return SkipRule(
            kind,
            "Not read inside archives",
            f"No archives pattern for {root.name} in keep.toml covers their archives any more.",
            root.name,
        )
    return SkipRule(
        NEXT_SCAN,
        "Read again at the next scan",
        "No rule leaves these out any more; the next scan reads them.",
        root.name,
    )


def _items_of(conn: Connection, resource_ids: Iterable[int]) -> dict[int, set[int]]:
    """The items linked to each of these files."""
    found: dict[int, set[int]] = {}
    ids = list(resource_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for rid, eid in conn.execute(
            select(EntityResource.resource_id, EntityResource.entity_id).where(
                EntityResource.resource_id.in_(chunk)
            )
        ):
            found.setdefault(rid, set()).add(eid)
    return found
