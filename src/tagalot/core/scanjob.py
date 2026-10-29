"""One complete scan of a root, in the order of DESIGN.md §6 steps 1-6.

:func:`scan_root` runs in a worker: file-system work happens on the calling thread, reads use
a read-only engine, and every write goes through the :class:`~tagalot.core.writer.DbWriter`.
Thumbnails (step 7) arrive in M8.
"""

import json
import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any

from sqlalchemy import Connection, Engine, select, update
from sqlalchemy.orm import aliased

from tagalot.core.archive_members import (
    ArchiveState,
    MemberCounts,
    MemberPlan,
    apply_listing,
    archive_patterns,
    archive_states,
    extracted,
    list_members,
    member_info,
    set_aside,
    wake_members,
)
from tagalot.core.archive_members import store_fingerprints as store_member_fingerprints
from tagalot.core.fingerprint import (
    FingerprintJob,
    FingerprintResult,
    compute_fingerprints,
    pending_fingerprints,
    store_fingerprints,
)
from tagalot.core.folder_items import fill_folders, folder_table, link_folder_fields
from tagalot.core.gallery_fill import (
    fill_galleries,
    fill_note_galleries,
    first_galleries,
    gallery_roles,
)
from tagalot.core.ingest import IngestSession, IngestWarning, forget_not_found
from tagalot.core.item_pictures import fill_note_pictures
from tagalot.core.keep import RootConfig
from tagalot.core.markdown_check import (
    check_markdown,
    doesnt_parse,
    is_markdown,
    record_markdown_problems,
)
from tagalot.core.models import ArchiveMode, Resource, ResourceKind, ResourceStatus, Root
from tagalot.core.note_keys import sync_aliases, sync_ignored
from tagalot.core.notes import apply_notes, note_rank, read_notes, used_notes
from tagalot.core.notes_folder import apply_folder_notes, plan_folder_notes
from tagalot.core.own_fields import OwnFields
from tagalot.core.own_notes import sync_own_fields
from tagalot.core.roots import RootCheck, check_root, local_path, record_root_check, sync_roots
from tagalot.core.scanner import (
    BATCH_SIZE,
    AppliedDiff,
    DirRule,
    Move,
    RootDiff,
    apply_diff,
    detect_moves,
    diff_root,
    in_notes,
    load_known,
    merge_applied,
    split_diff,
    walk_root,
    walk_scope,
)
from tagalot.core.see_through import counts_in, hidden_dir, logical
from tagalot.core.theme_options import effective_options
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.thumbnails.archive import ArchiveError
from tagalot.core.writer import DbWriter
from tagalot.themes.api import ResourceInfo, Theme

logger = logging.getLogger(__name__)

Progress = Callable[[str], None]
"""Receives short, human-readable status messages ("Walking Music…") during a scan."""

INGEST_BATCH = 100
"""Resources per ingest transaction: the theme's ingest runs inside the writer's transaction,
so batches stay small to keep the write lock short."""
PREPARE_THREADS = 8
"""Threads running the theme's ``prepare`` at once (#274): reading files is mostly waiting
(a share's latency) or decoding that releases the GIL, so threads overlap it."""
PREPARE_CHUNK = 10
"""Resources per ``prepare`` call, so one ingest batch is read by several threads."""

Prepared = tuple[list[ResourceInfo], dict[int, Any], list[tuple[str, str]]]
"""What reading some resources gave: those read (in order), their values, and the
``(relpath, error)`` of each that couldn't be read."""


@dataclass
class ScanReport:
    """What a scan did, for the activity panel and logs."""

    root_id: str
    online: bool
    error: str | None = None
    """Why the root was offline, if it was."""
    new: int = 0
    changed: int = 0
    restored: int = 0
    missing: int = 0
    skipped: int = 0
    """Known files the scan now leaves out (excludes, extensions): flagged, not missing."""
    unchanged: int = 0
    fingerprinted: int = 0
    moves: list[Move] = field(default_factory=list)
    read_errors: list[tuple[str, str]] = field(default_factory=list)
    """``(relative path, message)`` for folders and files that could not be read."""
    ingested: int = 0
    """Resources the theme ingested successfully."""
    ingest_errors: list[tuple[str, str]] = field(default_factory=list)
    """``(relative path, message)`` for resources whose ingest failed; they stay pending."""
    ingest_warnings: list[IngestWarning] = field(default_factory=list)
    members: MemberCounts = field(default_factory=MemberCounts)
    """What happened to files inside archives the user chose to look inside (#391)."""


def scan_root(
    writer: DbWriter,
    reader: Engine,
    root: RootConfig,
    path: str,
    *,
    extensions: Collection[str] | None = None,
    dirs: DirRule = False,
    when: datetime | None = None,
    check: Callable[[str], RootCheck] = check_root,
    theme: type[Theme] | None = None,
    schema: ThemeSchema | None = None,
    theme_options: Mapping[str, Any] | None = None,
    markdown_assets: str = "",
    progress: Progress | None = None,
    note_ignore_keys: frozenset[str] | None = None,
    note_key_aliases: Mapping[str, str] | None = None,
    notes_folder: str | None = None,
    see_through: frozenset[str] = frozenset(),
    own_fields: OwnFields | None = None,
) -> ScanReport:
    """Scan one root. ``path`` is this machine's path for it (after per-user overrides).

    An unreachable root is recorded as offline and its resources marked offline; nothing is
    deleted and the scan stops there. Otherwise the root is walked, diffed, and applied in
    short transactions; this root's unfingerprinted files are hashed (once each, so an
    unreadable file can't loop); moves among the new files are reattached; and, with a
    ``theme`` (and its ``schema``), every pending resource is ingested. The theme's
    extensions and directory rule then replace ``extensions`` and ``dirs``.

    ``theme_options`` are the keep's ``[theme.options]``; with the root's own options they
    give the values the theme sees, with the root's and the keep's (``markdown_assets``)
    default assets folders. When those differ from the values the root was last ingested
    with, every file of the root is ingested again.

    ``note_ignore_keys`` are the front-matter keys notes leave out (``core.note_keys``); when
    they differ from those the notes were last applied with, every note is read again.
    ``None`` keeps those. ``note_key_aliases`` are the user's key aliases (#411), the same
    way.

    ``notes_folder`` is the keep's notes folder when it is in this root (``""``: the whole
    root): only its Markdown files are recorded, and none of it reaches the theme.

    ``see_through`` are the keep's see-through folder names, case-folded (#459): such
    folders, and those inside them, never reach the theme, and their files reach it as
    their parent folder's (:attr:`ResourceInfo.folder`). When they differ from the names the
    root was last ingested with, every file of the root is ingested again.
    """
    if (theme is None) != (schema is None):
        raise ValueError("pass both theme and schema, or neither")
    if theme is not None:
        extensions = theme.extensions or None
        dirs = theme.dirs
    say: Progress = progress or (lambda message: None)
    when = when or datetime.now(UTC)
    writer.run(partial(_sync_one, root))

    say(f"Checking {root.name}…")
    reachability = check(path)
    writer.run(partial(_record_check, root.id, reachability))
    if not reachability.online:
        logger.warning("Skipping scan of %s: %s", root.id, reachability.error)
        return ScanReport(root.id, online=False, error=reachability.error)

    report = ScanReport(root.id, online=True)
    unreadable: list[str] = []

    def on_read_error(relpath: str, error: OSError) -> None:
        unreadable.append(relpath)
        report.read_errors.append((relpath, str(error)))
        logger.warning("Cannot read %s in root %s: %s", relpath or "(root)", root.id, error)

    say(f"Walking {root.name}…")
    entries = list(
        walk_root(
            path,
            exclude=root.exclude,
            extensions=extensions,
            dirs=dirs,
            on_error=on_read_error,
            notes=notes_folder,
            see_through=see_through,
        )
    )
    with reader.connect() as conn:
        scope = walk_scope(root.exclude, extensions, dirs, notes_folder, see_through)
        diff = diff_root(load_known(conn, root.id), entries, unreadable, scope)
    report.new, report.changed = len(diff.new), len(diff.changed)
    report.restored, report.missing = len(diff.restored), len(diff.missing)
    report.skipped = len(diff.skipped)
    report.unchanged = len(diff.unchanged)

    say(f"Updating {root.name}: {len(diff.new)} new, {len(diff.changed)} changed…")
    futures = [writer.submit(partial(_apply, root.id, part, when)) for part in split_diff(diff)]
    applied = merge_applied(f.result() for f in futures)

    with reader.connect() as conn:
        jobs = pending_fingerprints(conn, root_id=root.id, limit=None)
    if jobs:
        say(f"Fingerprinting {len(jobs)} files in {root.name}…")

    def job_path(job: FingerprintJob) -> str:
        return local_path(path, job.relpath)

    def on_hash_error(job: FingerprintJob, error: OSError) -> None:
        report.read_errors.append((job.relpath, str(error)))
        logger.warning("Cannot fingerprint %s in root %s: %s", job.relpath, root.id, error)

    batch: list[FingerprintResult] = []
    for result in compute_fingerprints(jobs, job_path, on_hash_error):
        batch.append(result)
        if len(batch) >= BATCH_SIZE:
            report.fingerprinted += writer.run(partial(_store, batch))
            batch = []
    if batch:
        report.fingerprinted += writer.run(partial(_store, batch))

    report.moves = writer.run(partial(_moves, applied))
    _sync_members(writer, reader, root, path, extensions, when, report, say)
    if theme is not None and schema is not None:
        options = effective_options(
            theme, theme_options or {}, root.options, (root.assets, markdown_assets)
        )
        for problem in options.problems:
            logger.warning("Root %s: %s", root.id, problem)
            report.ingest_warnings.append(IngestWarning(root.id, None, problem))
        defaults = effective_options(theme, {}, {}).fingerprint(1)  # recorded before versions
        fingerprint = _fingerprint(options.fingerprint(theme.version), see_through)
        if writer.run(partial(_options_changed, root.id, fingerprint, defaults)):
            say(f"Theme options changed: ingesting {root.name} again…")
        _ingest_pending(
            writer,
            reader,
            root,
            path,
            theme,
            schema,
            when,
            report,
            say,
            options.values,
            note_ignore_keys=note_ignore_keys,
            note_key_aliases=note_key_aliases,
            own_fields=own_fields,
            notes_folder=notes_folder,
            see_through=see_through,
        )
        writer.run(partial(_record_options, root.id, fingerprint))
    logger.info(
        "Scanned %s: %d new, %d changed, %d restored, %d missing, %d moved, %d fingerprinted, "
        "%d ingested, %d ingest errors",
        root.id,
        report.new,
        report.changed,
        report.restored,
        report.missing,
        len(report.moves),
        report.fingerprinted,
        report.ingested,
        len(report.ingest_errors),
    )
    return report


def _sync_members(
    writer: DbWriter,
    reader: Engine,
    root: RootConfig,
    path: str,
    extensions: Collection[str] | None,
    when: datetime,
    report: ScanReport,
    say: Progress,
) -> None:
    """Record the members of the root's archives that are read (#391,
    ``core.archive_members``): each such archive is listed again when it changed since it
    was last listed. Archives not read now take their members with them."""
    with reader.connect() as conn:
        states = archive_states(conn, root.id)
    patterns = archive_patterns(root.archives)
    reading: list[tuple[ArchiveState, ArchiveMode]] = []
    for archive in states:
        mode = archive.effective(patterns)
        if (
            archive.status is ResourceStatus.OK
            and not archive.skipped
            and mode is not ArchiveMode.NONE
        ):
            reading.append((archive, mode))
        elif archive.has_members or archive.listed is not None:
            writer.run(partial(set_aside, archive=archive))
    for number, (archive, mode) in enumerate(reading, start=1):
        if not archive.needs_listing():
            writer.run(partial(wake_members, archive_id=archive.id))
            continue
        say(f"Looking inside archives in {root.name}: {number} of {len(reading)}…")
        try:
            listing = list_members(local_path(path, archive.relpath), extensions)
        except (ArchiveError, OSError) as e:
            report.read_errors.append((archive.relpath, f"Can't look inside it: {e}"))
            logger.warning("Can't list %s in root %s: %s", archive.relpath, root.id, e)
            continue
        plan = MemberPlan(archive, mode, listing)
        counts = writer.run(partial(apply_listing, root_id=root.id, plan=plan, when=when))
        for name in ("new", "changed", "missing", "skipped", "undecided"):
            setattr(report.members, name, getattr(report.members, name) + getattr(counts, name))


# Writer jobs: module-level functions so each is a plain Callable[[Connection], T].


def _sync_one(root: RootConfig, conn: Connection) -> None:
    sync_roots(conn, [root])


def _record_check(root_id: str, check: RootCheck, conn: Connection) -> int:
    return record_root_check(conn, root_id, check)


def _apply(root_id: str, part: RootDiff, when: datetime, conn: Connection) -> AppliedDiff:
    return apply_diff(conn, root_id, part, when)


def _store(results: list[FingerprintResult], conn: Connection) -> int:
    return store_fingerprints(conn, results)


def _moves(applied: AppliedDiff, conn: Connection) -> list[Move]:
    return detect_moves(conn, applied.new_ids.values())


def _options_changed(root_id: str, fingerprint: str, defaults: str, conn: Connection) -> bool:
    """If the root was ingested with other option values, mark its files pending. A root
    with nothing recorded (new, or scanned before options existed) was ingested with the
    defaults, if at all."""
    stored = conn.scalar(select(Root.ingest_options).where(Root.id == root_id))
    if (stored if stored is not None else defaults) == fingerprint:
        return False
    marked = conn.execute(
        update(Resource)
        .where(Resource.root_id == root_id, Resource.ingested_at.is_not(None))
        .values(ingested_at=None)
    ).rowcount
    return bool(marked)


def _fingerprint(options: str, see_through: frozenset[str]) -> str:
    """What a root's files were ingested with: the theme's option values and, when there are
    any, the see-through folder names (#459), so a change to either reads them again."""
    if not see_through:
        return options  # as recorded before see-through folders
    return f"{options}|see_through={json.dumps(sorted(see_through))}"


def _record_options(root_id: str, fingerprint: str, conn: Connection) -> None:
    conn.execute(update(Root).where(Root.id == root_id).values(ingest_options=fingerprint))


# --- Ingest (DESIGN.md §6 step 5) ---


def ingest_pending(
    writer: DbWriter,
    reader: Engine,
    root: RootConfig,
    path: str,
    *,
    theme: type[Theme],
    schema: ThemeSchema,
    theme_options: Mapping[str, Any] | None = None,
    markdown_assets: str = "",
    when: datetime | None = None,
    progress: Progress | None = None,
    see_through: frozenset[str] = frozenset(),
) -> ScanReport:
    """Ingest the root's pending resources now, without walking it (re-reading chosen files,
    DESIGN.md §6). The root's theme options apply as in a scan."""
    report = ScanReport(root.id, online=True)
    options = effective_options(
        theme, theme_options or {}, root.options, (root.assets, markdown_assets)
    )
    _ingest_pending(
        writer,
        reader,
        root,
        path,
        theme,
        schema,
        when or datetime.now(UTC),
        report,
        progress or (lambda message: None),
        options.values,
        see_through=see_through,
    )
    return report


def _ingest_pending(
    writer: DbWriter,
    reader: Engine,
    root: RootConfig,
    path: str,
    theme: type[Theme],
    schema: ThemeSchema,
    when: datetime,
    report: ScanReport,
    say: Progress,
    options: Mapping[str, Any],
    *,
    reread_notes: bool = True,
    note_ignore_keys: frozenset[str] | None = None,
    note_key_aliases: Mapping[str, str] | None = None,
    notes_folder: str | None = None,
    see_through: frozenset[str] = frozenset(),
    own_fields: OwnFields | None = None,
) -> None:
    """Hand every pending resource of the root to the theme, in batches, then read the
    root's notes that changed (§9 *Notes*).

    A batch that fails rolls back and each of its resources is retried alone, so one bad
    file can't cost the rest; resources that still fail stay pending for the next scan.
    Notes are kept from a theme that doesn't take Markdown, and from every theme once they
    are an item's note.
    """
    if reread_notes and note_ignore_keys is not None:
        # Before the files: other ignored keys read Markdown files again too (#410).
        writer.run(partial(sync_ignored, keys=note_ignore_keys))
    if reread_notes and note_key_aliases is not None:
        writer.run(partial(sync_aliases, aliases=note_key_aliases))
    if reread_notes and own_fields is not None:  # the keep's own fields (#584)
        writer.run(partial(sync_own_fields, fields=own_fields))
    with reader.connect() as conn:
        rows = conn.execute(
            select(
                Resource.id,
                Resource.relpath,
                Resource.kind,
                Resource.ext,
                Resource.size,
                Resource.mtime_ns,
            )
            .where(
                Resource.root_id == root.id,
                Resource.status == ResourceStatus.OK,
                Resource.skipped.is_(False),
                Resource.ingested_at.is_(None),
                Resource.parent_resource_id.is_(None),
            )
            .order_by(Resource.relpath)
        ).all()
    hidden = {
        rid
        for rid, rel, kind, *_ in rows
        if kind is ResourceKind.DIR and hidden_dir(rel, see_through)
    }
    if hidden:  # see-through folders: never the theme's (#459)
        writer.run(partial(_mark_ingested, sorted(hidden), when))
    pending = [_resource(root.id, path, row, see_through) for row in rows if row[0] not in hidden]
    _check_markdown(writer, root, report, pending)
    pending, notes = _without_notes(reader, theme, pending, notes_folder)
    if notes:
        writer.run(partial(_mark_ingested, [r.id for r in notes], when))
    pending += _pending_members(reader, root, path, see_through)
    pending = _read_last(reader, root, path, theme, pending, see_through)
    _ingest_files(writer, reader, root, theme, schema, when, report, say, options, pending)
    if gallery_roles(schema):  # gallery roles filled from folders (#387)
        writer.run(
            partial(fill_galleries, schema=schema, root_id=root.id, notes_folder=notes_folder)
        )
    if folder_table(schema) is not None:  # folders of pictures as items (#447)
        writer.run(partial(fill_folders, schema=schema, root_id=root.id, see_through=see_through))
    if not reread_notes:
        return
    say(f"Looking for notes in {root.name}…")
    changes, problems = read_notes(reader, schema, root.id, path, see_through)
    report.ingest_warnings += problems
    if changes:
        again, problems = writer.run(
            partial(apply_notes, schema=schema, root_id=root.id, changes=changes)
        )
        report.ingest_warnings += problems
        if again:  # a note stopped giving a value: the files give theirs again
            writer.run(partial(_mark_pending, again))
            _ingest_pending(
                writer,
                reader,
                root,
                path,
                theme,
                schema,
                when,
                report,
                say,
                options,
                reread_notes=False,
                notes_folder=notes_folder,
                see_through=see_through,
            )
    # The pictures notes give their items (#439): even with no note changed, a picture
    # may have come or gone.
    writer.run(partial(fill_note_pictures, root_id=root.id, notes_folder=notes_folder))
    if folder_table(schema) is not None:  # what folders' notes say they belong to (#449)
        writer.run(partial(link_folder_fields, schema=schema, root_id=root.id))


def _pending_members(
    reader: Engine, root: RootConfig, path: str, see_through: frozenset[str]
) -> list[ResourceInfo]:
    """The root's members of archives waiting to be read (#391), as the theme gets them:
    counting in the archive's folder, to be extracted before they're read."""
    archive = aliased(Resource)
    with reader.connect() as conn:
        rows = conn.execute(
            select(
                Resource.id,
                Resource.relpath,
                Resource.kind,
                Resource.ext,
                Resource.size,
                Resource.mtime_ns,
                archive.relpath,
            )
            .join(archive, archive.id == Resource.parent_resource_id)
            .where(
                Resource.root_id == root.id,
                Resource.status == ResourceStatus.OK,
                Resource.skipped.is_(False),
                Resource.ingested_at.is_(None),
                archive.status == ResourceStatus.OK,
            )
            .order_by(Resource.relpath)
        ).all()
    return [
        member_info(
            root.id,
            (rid, rel, kind, ext, size, mtime),
            outer,
            local_path(path, outer),
            see_through,
        )
        for rid, rel, kind, ext, size, mtime, outer in rows
    ]


def _check_markdown(
    writer: DbWriter, root: RootConfig, report: ScanReport, pending: list[ResourceInfo]
) -> None:
    """Check the front matter of the Markdown files about to be read (#438): a key given
    twice, or front matter that doesn't parse. Their problems replace what they had, and
    are the scan's warnings."""
    files = [r for r in pending if r.kind == "file" and is_markdown(r.ext) and r.path]
    if not files:
        return
    found = {r.id: check_markdown(r.path) for r in files}
    writer.run(partial(record_markdown_problems, found=found))
    for resource in files:
        for problem in found[resource.id]:
            if not doesnt_parse(problem):  # that one is reported by whatever reads it
                report.ingest_warnings.append(IngestWarning(root.id, resource.relpath, problem))


def _without_notes(
    reader: Engine,
    theme: type[Theme],
    pending: list[ResourceInfo],
    notes_folder: str | None = None,
) -> tuple[list[ResourceInfo], list[ResourceInfo]]:
    """``pending`` without the notes the theme doesn't get (#379): note-named Markdown files
    it doesn't take (recorded only as notes), files that are items' notes, and everything
    in the notes folder (#382)."""
    accepts = theme.extensions
    held = {r.id for r in pending if in_notes(r.relpath, notes_folder)}
    candidates = [
        r
        for r in pending
        if r.kind == "file" and note_rank(logical(r.relpath, r.see_through)) is not None
    ]
    if candidates:
        with reader.connect() as conn:
            used = used_notes(conn)
        held |= {r.id for r in candidates if (accepts and r.ext not in accepts) or r.id in used}
    if not held:
        return pending, []
    return [r for r in pending if r.id not in held], [r for r in pending if r.id in held]


def _ingest_files(
    writer: DbWriter,
    reader: Engine,
    root: RootConfig,
    theme: type[Theme],
    schema: ThemeSchema,
    when: datetime,
    report: ScanReport,
    say: Progress,
    options: Mapping[str, Any],
    pending: list[ResourceInfo],
) -> None:
    """Hand ``pending`` to the theme in batches (see :func:`_ingest_pending`)."""
    ingester = theme()
    reads = type(ingester).prepare is not Theme.prepare
    batches = [pending[i : i + INGEST_BATCH] for i in range(0, len(pending), INGEST_BATCH)]
    if not batches:
        return
    # The next batch is read (on several threads) while the writer ingests this one.
    pool = ThreadPoolExecutor(PREPARE_THREADS, thread_name_prefix="prepare") if reads else None

    def start_reading(batch: list[ResourceInfo]) -> list[Future[Prepared]]:
        if pool is None:
            return []
        chunks = [batch[i : i + PREPARE_CHUNK] for i in range(0, len(batch), PREPARE_CHUNK)]
        return [pool.submit(_prepare, ingester, chunk, root) for chunk in chunks]

    # Members of archives are copied out for each batch, and the copies dropped once it's
    # ingested (#391).
    copies: dict[int, ExitStack] = {}

    def take_out(number: int) -> list[ResourceInfo]:
        stack = ExitStack()
        copies[number] = stack
        found = stack.enter_context(extracted(batches[number]))
        report.ingest_errors.extend(found.errors)
        if found.fingerprints:
            writer.run(partial(store_member_fingerprints, fingerprints=found.fingerprints))
        return found.resources

    try:
        ready = take_out(0)
        reading = start_reading(ready)
        for number in range(len(batches)):
            batch = ready
            done = (
                f"{number * INGEST_BATCH + len(batches[number])} of {len(pending)} in {root.name}"
            )
            prepared: dict[int, Any] = {}
            if pool is not None:
                say(f"Reading {done}…")
                batch, prepared = _gather(reading, report)
            if number + 1 < len(batches):
                ready = take_out(number + 1)
                if pool is not None:
                    reading = start_reading(ready)
            say(f"Ingesting {done}…")
            _ingest_batch(writer, ingester, schema, batch, when, prepared, options, report, root)
            copies.pop(number).close()
    finally:
        for stack in copies.values():
            stack.close()
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)


def _mark_ingested(ids: list[int], when: datetime, conn: Connection) -> None:
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start : start + BATCH_SIZE]
        conn.execute(update(Resource).where(Resource.id.in_(chunk)).values(ingested_at=when))


def scan_notes_folder(
    writer: DbWriter,
    reader: Engine,
    roots: Sequence[RootConfig],
    root_path: Callable[[str], str],
    notes_root: str | None,
    notes_path: str,
    *,
    theme: type[Theme],
    schema: ThemeSchema,
    theme_options: Mapping[str, Any] | None = None,
    markdown_assets: str = "",
    progress: Progress | None = None,
    see_through: frozenset[str] = frozenset(),
) -> ScanReport:
    """Match the keep's notes folder's notes to items and apply them (§9 *The notes
    folder*), after its roots were scanned, so a new item finds a note already there. With
    no notes folder (``notes_root`` ``None``), items let notes from one they had go. Items
    whose note stopped giving a value have their files read again at once."""
    report = ScanReport(notes_root or "", online=True)
    say: Progress = progress or (lambda message: None)
    say("Matching the notes folder's notes…")
    path = root_path(notes_root) if notes_root is not None else ""
    plan = plan_folder_notes(reader, schema, notes_root, notes_path, path)
    report.ingest_warnings += plan.warnings
    again, problems = writer.run(partial(apply_folder_notes, schema=schema, plan=plan))
    report.ingest_warnings += problems
    if first_galleries(schema):  # the pictures beside a note go to its item (#388)
        writer.run(
            partial(fill_note_galleries, schema=schema, root_id=notes_root, notes_path=notes_path)
        )
    if notes_root is not None:  # and the picture named like it, beside it (#439)
        writer.run(partial(fill_note_pictures, root_id=notes_root, notes_folder=notes_path))
    if not again:
        return report
    writer.run(partial(_mark_pending, again))
    with reader.connect() as conn:
        affected = set(conn.scalars(select(Resource.root_id).where(Resource.id.in_(again))))
    for root in roots:
        if root.id in affected:
            done = ingest_pending(
                writer,
                reader,
                root,
                root_path(root.id),
                theme=theme,
                schema=schema,
                theme_options=theme_options,
                markdown_assets=markdown_assets,
                progress=progress,
                see_through=see_through,
            )
            report.ingest_errors += done.ingest_errors
            report.ingest_warnings += done.ingest_warnings
    return report


def _mark_pending(ids: list[int], conn: Connection) -> None:
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start : start + BATCH_SIZE]
        conn.execute(update(Resource).where(Resource.id.in_(chunk)).values(ingested_at=None))


def _read_last(
    reader: Engine,
    root: RootConfig,
    path: str,
    theme: type[Theme],
    pending: list[ResourceInfo],
    see_through: frozenset[str] = frozenset(),
) -> list[ResourceInfo]:
    """``pending`` with the theme's ``read_last`` files after the others; when there are
    others, with every one of the root's ``read_last`` files, read again, so a library
    export meets the files added since (DESIGN.md §6, #320)."""
    late_extensions = frozenset(theme.read_last)
    if not late_extensions:
        return pending
    # A tuple or list reads its earlier extensions first (bibliographies before the notes
    # that cite them); a set, by path only.
    ordered = isinstance(theme.read_last, list | tuple)
    rank = {ext: n for n, ext in enumerate(theme.read_last)} if ordered else {}

    def order(resource: ResourceInfo) -> tuple[int, str]:
        return rank.get(resource.ext, 0), resource.relpath

    first = [r for r in pending if r.ext not in late_extensions]
    late = [r for r in pending if r.ext in late_extensions]
    if not first:
        return sorted(late, key=order)
    seen = {r.id for r in late}
    with reader.connect() as conn:
        rows = conn.execute(
            select(
                Resource.id,
                Resource.relpath,
                Resource.kind,
                Resource.ext,
                Resource.size,
                Resource.mtime_ns,
            )
            .where(
                Resource.root_id == root.id,
                Resource.status == ResourceStatus.OK,
                Resource.skipped.is_(False),
                Resource.parent_resource_id.is_(None),
                Resource.ext.in_(sorted(late_extensions)),
                Resource.ingested_at.is_not(None),
            )
            .order_by(Resource.relpath)
        ).all()
    late += [_resource(root.id, path, row, see_through) for row in rows if row[0] not in seen]
    late.sort(key=order)
    return first + late


def _resource(
    root_id: str, path: str, row: Sequence[Any], see_through: frozenset[str]
) -> ResourceInfo:
    """A stored resource as the theme gets it: with the folder it counts as in, inside a
    see-through folder (#459)."""
    rid, rel, kind, ext, size, mtime = row
    return ResourceInfo(
        rid,
        root_id,
        rel,
        kind.value,
        ext,
        size,
        mtime,
        local_path(path, rel),
        counts_in(rel, see_through) if kind is ResourceKind.FILE else None,
        see_through,
    )


def _ingest_batch(
    writer: DbWriter,
    ingester: Theme,
    schema: ThemeSchema,
    batch: list[ResourceInfo],
    when: datetime,
    prepared: Mapping[int, Any],
    options: Mapping[str, Any],
    report: ScanReport,
    root: RootConfig,
) -> None:
    """Ingest one batch; if it fails, each resource alone (see :func:`_ingest_pending`)."""
    if not batch:
        return
    try:
        _record(
            report,
            writer.run(partial(_ingest, ingester, schema, batch, when, prepared, options)),
            batch,
        )
    except Exception:
        for info in batch:
            try:
                _record(
                    report,
                    writer.run(partial(_ingest, ingester, schema, [info], when, prepared, options)),
                    [info],
                )
            except Exception as e:
                report.ingest_errors.append((info.relpath, f"{type(e).__name__}: {e}"))
                logger.warning("Ingest of %s in root %s failed: %s", info.relpath, root.id, e)


def _prepare(ingester: Theme, batch: list[ResourceInfo], root: RootConfig) -> Prepared:
    """Run the theme's ``prepare`` for some resources, on a reading thread (no database).
    If it raises, each resource is prepared alone; those that still fail are returned as
    errors and left out of the ingest, so they stay pending for the next scan."""
    try:
        return batch, dict(ingester.prepare(batch)), []
    except Exception:
        kept: list[ResourceInfo] = []
        prepared: dict[int, Any] = {}
        errors: list[tuple[str, str]] = []
        for info in batch:
            try:
                prepared.update(ingester.prepare([info]))
            except Exception as e:
                errors.append((info.relpath, f"{type(e).__name__}: {e}"))
                logger.warning("Reading %s in root %s failed: %s", info.relpath, root.id, e)
            else:
                kept.append(info)
        return kept, prepared, errors


def _gather(
    reading: list[Future[Prepared]], report: ScanReport
) -> tuple[list[ResourceInfo], dict[int, Any]]:
    """Wait for a batch's reading, and put its pieces back together in order. Errors are
    recorded here, on the scan's thread."""
    kept: list[ResourceInfo] = []
    prepared: dict[int, Any] = {}
    for future in reading:
        chunk, values, errors = future.result()
        kept.extend(chunk)
        prepared.update(values)
        report.ingest_errors.extend(errors)
    return kept, prepared


def _record(report: ScanReport, warnings: list[IngestWarning], batch: list[ResourceInfo]) -> None:
    report.ingested += len(batch)
    report.ingest_warnings.extend(warnings)


def _ingest(
    ingester: Theme,
    schema: ThemeSchema,
    batch: list[ResourceInfo],
    when: datetime,
    prepared: Mapping[int, Any],
    options: Mapping[str, Any],
    conn: Connection,
) -> list[IngestWarning]:
    """One ingest transaction: the theme's ingest, its flush, and marking the batch done."""
    ctx = IngestSession(conn, schema, prepared, options)
    forget_not_found(conn, [r.id for r in batch])
    ingester.ingest(batch, ctx)
    flushed = ctx.flush()
    conn.execute(
        update(Resource).where(Resource.id.in_([r.id for r in batch])).values(ingested_at=when)
    )
    return flushed.warnings
