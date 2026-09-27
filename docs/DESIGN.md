# Tagalot — Design Document

Status: draft v1 · Owner: project author · Implementation: Python (Rust reserved for isolated hot spots)

## 1. Overview

Tagalot is a desktop, tag-based file browser. Work is organized into **keeps**: self-contained projects that each have their own database, tag tree, list of watched directories ("roots"), and a **theme** that defines what kind of content the keep manages (music, 2D art assets, movies, …).

Tagalot never stores or moves user data. A keep holds only metadata and caches; the files themselves stay where they are, commonly on network shares (SMB/UNC paths, mapped drives, NAS mounts). This is why Tagalot is a native desktop app rather than a web app.

### Goals

- Browse and filter tens of thousands of items per keep with fast, hierarchical tag filtering (include and exclude).
- Make tagging fast: a filterable tag panel with drag-and-drop and keyboard application to one or many items.
- Plug-and-play themes: a programmer can add a new content type (entity classes, metadata fields, views, thumbnail logic, actions) by dropping a single Python file into a themes folder, without changing the application.
- Work well with network shares, including shares that are slow or temporarily offline.
- Never modify, move, rename, or delete the user's files.

### Non-goals (v1)

- Multi-user concurrent editing of one keep.
- Writing metadata back into files (for example, editing ID3 tags).
- Playing media or rendering fonts inside the app; files are handed to the OS or a configured program.
- Online metadata scraping (the theme API should leave room for it; see §14).

## 2. Glossary

| Term | Meaning |
|---|---|
| **Keep** | A folder containing one keep's config, database, and caches. Uses exactly one theme. |
| **Root** | A directory the keep watches. A keep has one or more roots. |
| **Resource** | A file or directory found under a root. Identified by root id + relative path. |
| **Entity** | A thing the user browses and tags (a Song, Album, Artist, Movie, Font…). Defined by the theme. May map to zero, one, or many resources. |
| **Role** | The function a resource plays for an entity ("audio", "cover", "photo", "screenshot", "folder"). |
| **Containment** | A parent/child relationship between entities (Artist ⊃ Album ⊃ Song), used for tag inheritance, "show contained items", breadcrumbs, and drill-down. |
| **Tag** | A node in the keep's hierarchical tag tree. Tags attach to entities. |
| **Theme** | A Python module that defines entity types, fields, roles, containment, ingestion, thumbnails, views, and actions. |
| **View** | A theme-declared screen: a search preset or an entity detail page. |

## 3. Technology choices

- **Python 3.12+**, managed with **uv**.
- **PySide6 (Qt 6)** for the UI. Qt's model/view classes render only visible rows, which keeps 50k-item lists responsive, and Qt provides drag-and-drop, docking, and `QDesktopServices` for opening files.
- **SQLite** (one database per keep) via **SQLAlchemy 2.0**: declarative ORM models (`Mapped`, `mapped_column`, `select()`) for core tables, and Core `Table` objects built at keep open for theme tables (§9). SQLModel was rejected because it fits neither the joined-table entity layout nor per-keep dynamic tables. Requires SQLite **3.45+ with FTS5** (for the trigram tokenizer's `remove_diacritics` option, §8); the app checks this at startup, before opening any window, by creating a trigram FTS5 table in an in-memory database (FTS5 can be compiled out of an otherwise recent SQLite), and shows a dialog explaining how to fix it if the check fails.
- **Pillow** for image decoding and thumbnails (including the PSD composite image).
- **mutagen** for audio metadata and embedded cover art.
- **Archives:** `zipfile` (stdlib), `py7zr` for 7z, `rarfile` for RAR (requires an external `unrar`/`bsdtar`; RAR support degrades gracefully when missing). All behind one internal `ArchiveReader` interface.
- **platformdirs** for per-user config/cache paths; **tomllib** / **tomli-w** for TOML.
- **hashlib.blake2b** (stdlib) for fingerprints.
- Tooling: **pytest**, **pytest-qt**, **ruff** (lint and format), **mypy**.

### Why Python, not Rust

The expected bottlenecks are directory scanning over network shares and thumbnail generation. Both are I/O-bound, so a faster language would not help much; running them in background workers does. Python also makes the plugin requirement straightforward: themes are importable modules. Rust has no stable ABI for dynamically loaded plugins, which would make drop-in themes much harder. If profiling identifies a CPU-bound hot spot (hashing, walking, image decoding), that function can be moved to Rust via PyO3 behind the same Python interface.

## 4. The keep on disk

```
MyMusic.keep/
  keep.toml        # identity, theme, roots (shared, portable)
  keep.db          # SQLite: resources, entities, tags, theme tables
  thumbs.db        # SQLite: thumbnail blobs (disposable cache)
  ui_state.json    # view toggles, column widths, saved layout (optional)
```

### keep.toml

```toml
[keep]
id = "0b6e3c1e-6f0a-4b54-9a8e-2b2d7f1c9d41"   # UUID, never changes
name = "Music"
format_version = 1                          # core schema version

[theme]
id = "music"
version = 1                                 # theme schema version

[[roots]]
id = "nas-music"                            # stable, referenced by resources
name = "NAS music share"
path = '\\nas\music'
exclude = ["**/.DS_Store", "**/Thumbs.db", "**/@eaDir/**"]
```

### Rules

- **Resources store root id + relative POSIX-style path**, never absolute paths. Changing a root's path (new drive letter, new server) requires editing one value.
- **Per-machine root overrides.** The per-user settings file (see below) can map `(keep id, root id) → local path`, so the same keep opened on two machines can reach a share through different mount points.
- **Offline is not deleted.** If a root is unreachable at scan time, its resources are marked offline and nothing is removed. Resources are only marked missing when their root is reachable and the file is gone.
- **Keep location.** `keep.db` should live on a local disk. SQLite locking over SMB is unreliable. The app warns (but does not refuse) when a keep is opened from a network path, and a keep is single-user in v1. Detection is best effort and never blocks opening: on Windows, UNC paths and drives that `GetDriveTypeW` reports as remote (mapped drives); on Linux and macOS, the mount's file-system type (nfs, cifs/smbfs, afpfs, sshfs, and similar; WSL's `/mnt/c` counts as local).
- **Keeps and roots never contain each other.** Creating a keep inside one of its roots, or with a root inside the keep folder, is refused: the scanner would index the keep's own files.
- **Creating a keep** requires a new or empty folder and writes `keep.toml` with a fresh UUID; the databases are created on first open.
- SQLite runs in **WAL mode** with a `busy_timeout` for local keeps. Keeps on a network path use the rollback journal (`journal_mode=DELETE`, `synchronous=FULL`) instead, because WAL needs shared memory on one host and does not work over network file systems. Every connection turns foreign keys on. Connections for UI queries set `query_only`, so only the DB writer can write. SQLAlchemy, not Python's `sqlite3` module, issues `BEGIN`, so DDL is transactional and a failed migration rolls back cleanly.
- **keep.toml is hand-editable.** Tagalot writes strings as TOML literal strings (`'\\nas\music'`) so paths need no escaping, falling back to escaped basic strings only when a value contains `'` or a control character. Root paths are kept exactly as written (a string, not a parsed path), because a keep written on Windows may be read on another OS. Writes are atomic (temp file, then replace).
- **Unknown keys in keep.toml are ignored with a logged warning**, so an older build can still open a keep written by a newer one. They are not preserved when Tagalot rewrites the file.

### Per-user settings (not in the keep)

Stored in `platformdirs.user_config_dir("tagalot", appauthor=False)/settings.toml` (on Windows `%LOCALAPPDATA%\tagalot\settings.toml`; deliberately not a roaming location, since the contents are machine-specific):

- Recent keeps: keep folders, most recent first, at most 10, compared case-insensitively on Windows.
- Per-machine root path overrides: `[root_overrides.<keep id>]` tables mapping root id → local path.
- File-handler overrides (§11), because application paths differ by machine: `[[handlers]]` tables with `ext`, optional `role`, and `command`. A rule for an extension and role wins over one for the extension alone. Commands may only use the `{path}`, `{dir}`, and `{name}` placeholders.
- Extra theme search directories (`theme_dirs`).

Settings never block startup: a missing file means defaults; a file that cannot be parsed is renamed to `settings.toml.invalid` and defaults are used; individual invalid entries are skipped. Every problem is logged.

## 5. Data model

All core tables are created and owned by the core. Theme tables are prefixed with the theme id (`music_song`, `movies_actor`).

Conventions for core tables:

- **Timestamps** are stored in UTC and read back as timezone-aware datetimes; naive datetimes are rejected.
- **Enumerated columns** (`resource.kind`, `resource.status`, `field_provenance.source`) are stored as text with a CHECK constraint on the allowed values.
- **Deletes:** link rows (`entity_resource`, `entity_contains`, `entity_ancestor`, `entity_tag`, `field_provenance`, `tag_alias`) cascade with the entity, resource, or tag they link. Deleting a resource clears any `entity.thumb_resource_id` that pointed at it. Deleting a tag that has children, or a root that still has resources, is refused by the database; the tag delete operation (§7) and root removal handle those explicitly. Deleting an entity never deletes a resource.

### Core tables

**root** — mirrors keep.toml for joins and status.
`id (text pk)`, `name`, `online (bool)`, `last_scan_at`, `last_error`.

**resource** — a file or directory under a root.
`id (int pk)`, `root_id`, `relpath` (POSIX), `kind` (`file`/`dir`), `ext` (lowercase), `size`, `mtime_ns` (integer nanoseconds, compared exactly when diffing), `fingerprint` (nullable), `status` (`ok`/`offline`/`missing`), `first_seen_at`, `last_seen_at`, `parent_resource_id` (nullable; reserved for archive members, §14).
Unique `(root_id, relpath)`. Index on `fingerprint`.

**entity** — base table for every theme entity (joined-table layout: each theme type's table shares its `id`).
`id (int pk)`, `type` (namespaced discriminator such as `music.song`), `title` (display name, denormalized for fast listing and sorting), `ingest_key` (nullable text; the theme's stable natural key for the entity, §9), `extra` (JSON, user-defined fields), `created_at`, `updated_at`, `thumb_resource_id` (nullable; memoized thumbnail source).
Unique `(type, ingest_key)`. The key lets re-ingest find an existing entity even after the user renames its title.

**entity_resource** — role-based links.
`entity_id`, `resource_id`, `role`, `sort_order`.
PK `(entity_id, resource_id, role)`. Index on `resource_id`.

**entity_contains** — containment edges declared by the theme's ingester.
`parent_id`, `child_id`. PK `(parent_id, child_id)`. Containment is a DAG, not a strict tree (a song may appear on a studio album and a compilation). Cycles are rejected.

**entity_ancestor** — closure table maintained by the core.
`entity_id`, `ancestor_id`, `depth`. Every entity has a self row at depth 0. PK `(entity_id, ancestor_id)`; index `(ancestor_id, entity_id)`. When a DAG gives two paths, keep the minimum depth.

**tag** — `id`, `parent_id` (nullable), `name`, `color` (nullable), `sort_order`. Sibling names are unique case-insensitively via a unique expression index on `(coalesce(parent_id, 0), lower(name))`; the `coalesce` is needed because SQLite treats NULLs as distinct in unique indexes, which would otherwise allow duplicate root-level tags (tag ids start at 1, so `0` is a safe sentinel). Tag operations in `tags.py` also check for clashes before writing so the UI can show a clear message; the index is the backstop. SQLite's `lower()` folds ASCII only, so the app-level check compares with `str.casefold()`. Names may repeat under different parents; the UI shows the full path when ambiguous.

**tag_alias** — `tag_id`, `alias`. Used only for matching in the tag filter box.

**entity_tag** — `entity_id`, `tag_id`, `added_at`. PK `(entity_id, tag_id)`; index `(tag_id, entity_id)`. Only directly applied tags are stored; parent tags are never stored implicitly.

**field_provenance** — `entity_id`, `field`, `source` (`extracted`/`user`/`fetched`), `updated_at`. Extraction and fetching never overwrite a field whose provenance is `user` unless the user explicitly requests a refresh.

**saved_search** — `id`, `name`, `definition` (JSON of the search model in §8).

**schema_version** — `component` (`core` or theme id), `version`.

**Core schema versions** follow the same rule as theme versions (§9). The core version is kept both in `keep.toml` (`format_version`) and in `schema_version`. On open: a newer version in either place is refused with a clear message before anything is changed; an empty database gets the core tables and a `core` row; an older database raises a distinct "needs migration" error so the launcher can ask the user. Once the user confirms, `keep.db` is copied with SQLite's backup API (safe with WAL) to `keep.db.v<old>-<timestamp>.bak`, every migration step runs in one transaction (all or nothing), and `keep.toml` is updated. The backup is kept even if the migration fails.

**entity_fts** — FTS5 virtual table for text search (§8), one row per entity, `rowid = entity.id`. Columns `title` and `body` (the entity's `search="text"` field values plus `extra` values, joined with spaces). Tokenizer `trigram remove_diacritics 1`. Maintained by the DB writer, not triggers.

### Theme tables

Each theme entity type has its own table whose primary key `id` is also a foreign key to `entity.id` (joined-table layout). Its columns are real, typed columns (dates, numbers, strings) so they sort, index, and range-filter correctly. Theme classes are plain declarations; the core builds these tables as SQLAlchemy Core `Table` objects when a keep is opened (§9 Mapping and isolation). They are not ORM-mapped classes. Core tables are ordinary module-level ORM models. Relationship link tables declared with `related()` (§9) are built the same way, named `<theme id>_<relationship name>`.

### Fingerprints

`fingerprint = blake2b(size || first 64 KiB || last 64 KiB)`, computed lazily in background workers for files only. Precisely: a 16-byte (128-bit) blake2b digest over the size as 8 bytes little-endian, then the first and last 64 KiB; files of at most 128 KiB are hashed whole, once. Workers skip files whose size or mtime changed since they were queued, and the stored value is written only if the row's size and mtime still match what was hashed, so a fingerprint never attaches to a newer version of a file. Files are queued newest first so move detection has what it needs. It is used to:

1. Reattach entities to files that were moved or renamed (a "new" resource whose fingerprint matches a "missing" one takes over its links; §6).
2. Find exact duplicates for the dedupe view (§13).

It is not a cryptographic identity of the whole file; when an exact match matters (dedupe merge), the UI can offer a full-hash verification.

## 6. Scanning and ingestion

A scan runs per root in background workers:

1. **Reachability check.** If the root is unreachable, mark the root offline and its resources `offline`, then stop. A root is reachable when its path on this machine (after per-user overrides) is a folder that can be listed within 10 seconds; an unreachable share can otherwise block for a minute. Missing paths, non-folders, permission errors, and timeouts all count as offline, each with its own `root.last_error`. Only `ok` resources become `offline`; `missing` ones stay `missing`, and nothing is deleted. When the root is reachable again, the scan's diff sets each resource back to `ok` or `missing`.
2. **Walk** with `os.scandir`, applying root exclude patterns and the theme's accepted extensions and directory rules. Directories are recorded as resources when the theme asks for them (for example, album folders). Details:
   - **Exclude patterns** match the POSIX relative path, case-insensitively on every OS (so a portable `keep.toml` behaves the same everywhere). `*` and `?` stay within one path segment, `**` spans segments, `**/x` also matches `x` at the top level, and `x/**` matches the folder `x` itself, so excluded folders are pruned without being read.
   - **Symlinked folders and junctions are not followed** (no loops, no escaping the root); symlinked files are included.
   - **Unreadable folders or files** are reported with their relative path and skipped; the walk continues.
   - Entries are yielded in name order within each folder, so a walk is deterministic.
3. **Diff** against the database by `(root_id, relpath)`:
   - New path → insert resource.
   - Existing path with changed `size`/`mtime` → update and clear fingerprint (recomputed later) and invalidate thumbnails.
   - Path not seen → mark `missing`.
   - A path seen again after being `offline` or `missing` → back to `ok`, keeping its id (and its fingerprint if size and mtime are unchanged).
   - Paths at or under a folder the walk could not read are left as they are, not marked `missing`: not being able to look is not the same as the file being gone.
   - `last_seen_at` moves only for resources the walk actually saw; `root.last_scan_at` records the completed scan. Nothing is deleted.
   - The diff itself is a pure function of the stored rows and the walk; the DB writer applies it in batches and gets back the new resource ids for move detection and ingest.
4. **Move detection.** For new resources whose fingerprint matches a resource marked missing, transfer entity links to the new resource and delete the stale row. Fingerprints are computed for new files first so this step has what it needs. Rules:
   - A *new* resource is an `ok` file with a fingerprint and no entity links yet; a new file that already has links is a copy, not a move, and is left for dedupe (§13).
   - Missing resources in any root match, with no time limit (a file restored from a backup months later is still the same file). A missing file that was never fingerprinted cannot be matched.
   - When several missing and new resources share a fingerprint, they are paired only where the file name decides uniquely; anything still ambiguous is logged and left alone rather than guessed.
   - Role links, `entity.thumb_resource_id`, archive members, and the original `first_seen_at` move to the new resource.
5. **Ingest.** New and changed resources are passed to the theme's ingester in batches. The ingester creates or updates entities, role links, containment edges, and extracted field values (respecting provenance).
6. **Closure maintenance.** The core updates `entity_ancestor` for changed containment edges.
7. **Thumbnail queue.** Affected entities are queued for thumbnail resolution (§10).

### Closure maintenance

`entity_ancestor` stores minimum depths over a DAG, so removing an edge can drop an ancestor or lengthen its shortest path, and the table alone cannot tell which (path counts would fix membership but not minimum depth). The core therefore uses one strategy for every containment change (edge add/remove, entity delete, entity merge, reparent):

1. **Collect affected nodes** before changing edges: for each changed edge `p → c`, all descendants of `c` (including `c`) from the current closure. Only these can gain or lose ancestors; rows whose ancestor is itself inside the affected set cannot depend on the changed edge (that would require a cycle).
2. **Apply the edge changes** to `entity_contains`.
3. **Recompute** the affected nodes' rows: delete them, then rebuild them with one recursive CTE that walks `entity_contains` upward and keeps `min(depth)` per `(entity_id, ancestor_id)`. The CTE has a depth cap (64) as a guard against corrupt data.

Because recomputation reads edges, not the closure, the order of changes within a batch does not matter. Performance: an edge into an entity with no children can't close a cycle, so the recursive cycle check runs only when the child already has children; accepted edges are inserted in batches. During ingest most children are new leaves, which took adding 52k edges from 20 s to 0.7 s. The DB writer calls `closure.apply(added, removed)` once per batch, in the same transaction as the edge changes. Containment is shallow, so affected sets are small (one song, or an album plus its tracks). An incremental upsert for additions is a possible later optimization, not part of v1.

- **Cycle check:** an added edge `p → c` is rejected if `p == c` or `c` is already an ancestor of `p`; edges in a batch are checked in order, each against the graph including the previously accepted ones. A rejected edge (a theme ingester bug) is logged with the resource path, surfaced in the activity panel, and skipped; the scan continues.
- **`rebuild_all()`** recomputes the whole table. It is exposed as a repair action and is the oracle for tests: randomized sequences of adds and removes must leave the incrementally maintained table identical to a full rebuild. **`verify()`** computes the correct table with the same query and reports missing and extra rows without changing anything. **`maintenance.repair_indexes()`** is the one repair action for derived data: it checks, then rebuilds, both the closure and the search index, and reports what was wrong.
- **Depth semantics:** because depth is the minimum, `depth = 1` means exactly "direct child" and `depth = 0` is the self row.
- **API** (`core/closure.py`): `apply(conn, added, removed)` applies removals first, then additions in order (each checked for cycles against the edges as they stand, including earlier additions in the batch), and returns what was added, removed, and rejected with a reason. Existing edges and missing ones are skipped. `add_entities(conn, ids)` gives new entities their depth-0 self row, which inheritance queries need even without edges. `detach(conn, ids)` removes every edge touching the entities and must run before they are deleted: the foreign-key cascades remove their own rows but not transitive ones (in A → B → C, deleting B would leave C under A). Affected ids go through a temporary table, so batch size isn't limited by SQLite's bound-parameter limit.

`core/scanjob.py`'s `scan_root()` runs steps 1–4 for one root in a worker and returns a report (counts, moves, read errors) for the activity panel. Within a scan, each pending file of the root is fingerprinted at most once, so an unreadable file can't loop, and move detection considers only that scan's new files. Steps 5–7 join it with themes (M4) and thumbnails (M8).

Scans are incremental and resumable. File-system watchers are not relied upon because they are unreliable on SMB; rescans are manual or scheduled per keep.

### Threading model

- Workers (a `QThreadPool`) do file-system I/O, hashing, metadata extraction, archive reading, and image decoding.
- Workers never write to the database directly. They send result batches to a single **DB writer** that applies them in short transactions, which avoids SQLite write contention.
  The writer (`core/writer.py`) is one thread that owns the write engine. A job is a function taking a connection; each runs in its own transaction, in submission order, and the submitter gets a `Future` for its result (the UI converts these to signals). A failed job rolls back only itself. Large scans are split into parts of at most 500 changes, each its own transaction; an interrupted scan is completed by the next one because the diff is recomputed.
- Read queries for the UI use their own connection. Anything that may take more than a few milliseconds (large searches, counts for the dashboard) runs off the GUI thread and posts results back via signals.
- The GUI thread must never block on file-system or network I/O.

## 7. Tags

- Tags form a tree. Depth is expected to be shallow (a handful of levels), and tag counts in the hundreds to low thousands. The full tree is cached in memory and invalidated on change. The cache holds an immutable snapshot (`TagTree`: children in display order, memoized subtrees, paths, aliases, case-insensitive sibling lookup, filter-box matching); tag operations invalidate it after they commit, and readers holding an older snapshot are unaffected.
- **Search semantics:** selecting a tag means "this tag or any of its descendants."
- **Applying a child tag never stores its parent.** Expansion at query time makes the parent match.

### Tag operations (tag manager)

| Operation | Behavior |
|---|---|
| Add | Create under the selected tag or at root level. |
| Rename | Must stay unique among siblings (case-insensitive). |
| Reparent | Drag-and-drop or "Move to…" picker. Moving a tag under itself or a descendant is rejected. Show the affected item count. |
| Merge | Fold tag A into tag B: reassign all `entity_tag` rows (deduplicating), move A's children under B, add A's name as an alias of B, delete A. |
| Delete | If the tag has children, ask: delete the subtree, or move children up to the deleted tag's parent. Show how many entities lose a tag. |
| Aliases | Alternate names that match in the filter box only. |
| Color | Optional, shown on chips. |

Details the table leaves open:

- Names are trimmed with inner runs of spaces collapsed, must be non-empty and at most 200 characters, and compare with `str.casefold()` among siblings. Renaming a tag to a different case of its own name is allowed. Colors are `#rrggbb`.
- New and moved tags go to the end of their new parent's children.
- **Reparent** is also refused when the new parent already has a child with the same name.
- **Merge** is refused into the tag itself or one of its descendants (that would create a cycle); merging into an ancestor is fine. A child of A whose name clashes with a child of B is merged into it recursively, since merging is what the user asked for. A's aliases move to B along with A's name, skipping any that duplicate B's name or aliases.
- **Delete, promoting children:** refused, with the clashing names listed, if a child's name clashes with a tag at the level above; the user didn't ask for a merge, so none is done silently. Deleting a subtree removes its tags from entities; entities are never deleted.
- Each operation re-reads the tree inside its own transaction to validate, never trusting a possibly stale cache, and the cache is invalidated after it commits.

All tag operations are single transactions and are recorded in an undo stack for the session.

**Undo/redo** (`core/tag_service.py`): each operation's transaction also records the full `tag` and `tag_alias` tables before and after it (they are small, and only tag operations write them) and the `entity_tag` rows it removed and added within its scope (both subtrees for a merge, the subtree for a delete). Undo restores the "before" tables and reverses that delta; redo restores "after" and replays it. Neither re-runs the operation, so redo can't fail validation. Tagging done after an operation is kept when it is undone; the one exception is undoing the creation of a tag, which removes that tag (and so its uses) again. The history holds 100 steps and is cleared by a new operation after an undo, as usual.

## 8. Search

### The search model

A search is an immutable, hashable value object, serializable to JSON (used for saved searches and view state). Tuples rather than lists keep it hashable, so it can key result caches.

```python
@dataclass(frozen=True)
class SearchSpec:
    types: tuple[str, ...] = ()           # entity types in scope; empty = all
    include: tuple[int, ...] = ()         # tag ids; each must match (AND)
    exclude: tuple[int, ...] = ()         # tag ids; any match removes the item
    fields: tuple[FieldFilter, ...] = ()  # typed filters, only for fields every scoped type has
    within: int | None = None             # entity id: restrict to its descendants
    text: str | None = None               # matches title and text-search fields (trimmed; blank = None)
    inherit_tags: bool = False            # tags on ancestors count
    show_contained: bool = False          # also list descendants of matching containers
    aggregate_up: bool = False            # containers match if any descendant matches
    sort: tuple[SortKey, ...] = (SortKey("title"),)

FieldFilter = TextFilter(field, text, match) | RangeFilter(field, low, high) | ChoiceFilter(field, values)
SortKey(field, descending=False)
```

One filter kind per `field(search=...)` kind (§9): `TextFilter` compares case-insensitively, with `match` either `contains` (anywhere in the value; the default) or `starts_with` (at the start of the whole value, so "The" matches "The Beatles" but not "Abbey Road: The Remaster"); `RangeFilter` bounds are inclusive and either may be open (`None`); `ChoiceFilter` matches any of its values. The JSON form carries `"version": 1`; dates and datetimes are tagged (`{"$date": "1997-09-22"}`) so they round-trip as dates; missing keys take defaults and unknown keys are ignored; a newer version is refused with a clear message.

### Semantics

- Each included tag is expanded to itself plus descendants, forming one group; **every group must match** (AND across groups, OR within a group).
- All excluded tags are expanded and merged into one set; **matching any excludes the item**.
- **Inheritance (`inherit_tags`)** changes matching: an entity's effective tags are its own plus those of all its ancestors. This applies to exclusion too: excluding "Christmas" removes every song of an album tagged Christmas.
- **Show contained (`show_contained`)** changes display: descendants of matching containers are also listed (as expandable children in tree/grouped layouts).
- **Aggregation (`aggregate_up`)**: a container matches if any descendant matches. Off by default; exposed as an advanced toggle.
- **Within** restricts results to descendants of one entity (used by drill-down chips and container detail pages).

How they combine (the design above leaves these open):

- **Within** means strict descendants (depth ≥ 1); the entity itself is never listed.
- **Aggregation** lets a container match on tags and text through a descendant that is not itself excluded. Field filters always apply to the listed entity, since they belong to its type, and the container's own exclusion always applies: an excluded album is not brought back by a matching song. With no include tags and no text there is nothing to aggregate, and the toggle has no effect.
- **Show contained** lists the matches plus every descendant of a match, regardless of `types` (that is the point: search albums, see their songs) and without re-applying include, text, or field filters to those descendants, which are shown because of their container. Exclusion still applies to them, as §8 says matching any excluded tag removes the item. Each entity is listed once even when two matching containers share it.

- **Deleted tags** (for example in a saved search) count as a subtree of just themselves: including one matches nothing, excluding one excludes nothing.
- **Field filters** resolve field names through a mapping of columns: the core provides `title`, `created_at`, and `updated_at`; themes add theirs (§9). An unknown field is an error, never ignored.
- **Sorting** follows `sort`, compares titles case-insensitively, and always ends with the entity id, so paging never skips or repeats an item.

### Query shape

With inheritance, each include group becomes:

```sql
EXISTS (
  SELECT 1 FROM entity_ancestor a
  JOIN entity_tag t ON t.entity_id = a.ancestor_id
  WHERE a.entity_id = e.id AND t.tag_id IN (:group)
)
```

Without inheritance, add `AND a.depth = 0` (or query `entity_tag` directly). Exclusion is the same pattern under `NOT EXISTS` with the merged exclude set. Field filters become ordinary `WHERE` clauses on the joined theme tables. SQL is built with SQLAlchemy Core expressions, never string concatenation.

### Text search

The search box uses the `entity_fts` table (§5) with the FTS5 **trigram** tokenizer, so text matches **substrings** ("bey" finds "Abbey"), case-insensitively and ignoring diacritics ("beyonce" finds "Beyoncé").

- **Query building:** the user's input is split on whitespace; each term is double-quoted (internal quotes doubled) and the terms are ANDed. Users never see FTS query syntax, and stray quotes or words like `AND` cannot cause syntax errors. Before that, `SearchSpec` cleans the text: control characters (including NUL, which FTS5 treats as the end of its query) become spaces and unpaired surrogates are dropped.
- **Short terms:** trigram matching needs at least 3 characters. Terms of 1–2 characters fall back to a `LIKE` scan over `entity_fts` (about 10 ms at 50k entities), with `%` and `_` escaped. `LIKE` ignores case for ASCII letters only and does not fold diacritics, so a 2-letter non-ASCII term is matched exactly.
- **Sync:** whenever an entity's title, text-search fields, or `extra` change (ingest, user edit, merge, delete), the DB writer rewrites that entity's `entity_fts` row in the same transaction (`core/fts.py` `sync_entities`). `body` is the text-search field values then the `extra` values, flattened: nested lists and objects contribute their values (not keys), numbers become text, booleans and nulls are skipped. Theme text fields arrive through a callback once themes exist. A "Rebuild search index" action (`rebuild_search_index`) repopulates the table from scratch, and `index_is_consistent` is a cheap health check.
- **Field filters** (a `search="text"` field in the filter bar) use ordinary `LIKE` on their own column; FTS is only for the global text box.
- **Size:** measured at roughly 20 MB of index per 50k entities (about 5× a word-based index). This is accepted; it is small next to `thumbs.db`.

### Global vs. scoped searches

- The core always provides a **global search** over all entity types. Results are grouped by type in collapsible sections with counts.
- Themes declare **scoped search presets** (Actors, Fonts, Albums). Field filters appear only when every type in scope has the field.
- Each view has defaults for `inherit_tags` and `show_contained`; user toggles override them and are saved per view in `ui_state.json`.
- Saved searches appear in the navigation list.

### Performance target

Tag + field searches over 50,000 entities return the first page in under 100 ms on a local SSD. Results are paged/lazily fetched by the Qt model.

Measured (2026-09, dev machine, `tests/core/test_search_benchmark.py`, 50,500 entities: 500 artists × 10 albums × 9 songs, 300 tags applied at every level, a DAG via compilations), median first page: everything by title 6 ms; a 90-tag subtree 12 ms; two groups 17 ms; excluding a subtree 17 ms; text 5 ms (2-letter `LIKE` 35 ms); inherited include/exclude 38/47 ms; within 1 ms; aggregate 5 ms; show contained 20 ms; all options at once ~105 ms. Counts cost about the same as a page. The benchmark asserts five times the target, so CI runners don't flake but real regressions fail.

What makes it fast: every test against a set (a tag group, the exclusion set, the text matches, the closure) is an uncorrelated `IN (subquery)`, so SQLite builds the set once from the `(tag_id, entity_id)` and `(ancestor_id, entity_id)` indexes; each distinct set is a `MATERIALIZED` CTE computed once per query; and aggregation first finds the (small) set of matching descendants, then their ancestors through the closure's key. The first version used correlated `EXISTS` and took 3.3 s for show-contained and 6.1 s for everything at once.

## 9. Theme API

### Discovery

- Built-in themes ship in `tagalot/builtin_themes/`.
- User themes are single `.py` files (or packages) in `user_data_dir("tagalot")/themes/` and in any extra directories from settings. They are loaded with `importlib`.
- Later, installed packages can register themes via the `tagalot.themes` entry-point group.
- A theme module exposes exactly one `Theme` subclass. Loading errors are reported in the keep launcher and never crash the app.
- A theme only imports from `tagalot.themes.api`, the stable public surface. Everything else in `tagalot` is internal.

### What a theme declares

```python
from datetime import date
from tagalot.themes.api import (Theme, Entity, field, role, contains, related,
                                SearchView, DetailView, Section, action)

class Actor(Entity):
    title_label = "Name"           # relabels the core `entity.title` column
    born:    date | None = field("Date of birth", search="range")
    country: str | None  = field("Country", search="choice")
    roles = [role("photo", kinds={"image"}, many=False, thumbnail=True)]

class Collection(Entity):
    roles = [role("folder", kinds={"dir"}, primary=True)]

class Movie(Entity):
    year: int | None = field("Year", card=True, search="range")
    roles = [role("video", kinds={"video"}, many=True, primary=True),
             role("poster", kinds={"image"}, thumbnail=True),
             role("screenshot", kinds={"image"}, many=True)]

class MoviesTheme(Theme):
    id, name, version = "movies", "Movies", 1
    extensions = {".mkv", ".mp4", ".avi", ".jpg", ".png"}
    entities = [Actor, Collection, Movie]
    containment = [contains(Collection, Movie)]
    relationships = [related("cast", Actor, Movie, label="Cast", reverse_label="Filmography")]
    views = [
        SearchView("Movies", types=[Movie], inherit_tags=True, show_contained=False),
        SearchView("Actors", types=[Actor]),
        DetailView(Movie, sections=[Section.fields(), Section.role("poster"),
                                    Section.related("cast"), Section.gallery("screenshot")]),
    ]

    def ingest(self, batch, ctx): ...          # resources -> entities, links, fields (via ctx)
    def thumbnail_chain(self, entity_type): ...  # ordered providers (§10)
    def similarity(self, a, b) -> float | None: ...  # optional, for dedupe (§13)
    def migrate(self, from_version, ctx): ...      # theme schema upgrades
```

(Exact names are illustrative; the implementation should keep this shape.)

Theme modules do not import SQLAlchemy. Entity classes are plain declarations; table names (`movies_actor`) and type ids (`movies.actor`) are derived from the theme id and the lowercased class name, overridable with `table_name` / `type_id` class attributes.

### Mapping and isolation

Importing a theme module never touches a database or a SQLAlchemy registry. This keeps themes isolated from each other and from the app:

- **At startup (launcher):** every theme module is imported and its declarations are validated without a database: unique theme id, valid field types and `search` kinds, at most one primary role per type, containment and relationships referencing declared types, views referencing declared roles and relationships. Problems are shown in the launcher next to the theme and never crash the app. The launcher also compares the theme version with each keep's `keep.toml`.
- **When a keep opens:** the core builds a fresh `MetaData` holding the theme's entity tables and relationship link tables as SQLAlchemy Core `Table` objects (each entity table's `id` references the core `entity.id` column directly), then creates or migrates them. Errors at this stage (for example, a table that cannot be built) are reported for that keep only; other keeps and themes are unaffected.
- **Consequences:** a broken theme affects only keeps that use it; a theme can be reloaded without restarting the app (useful while developing one); each test builds a clean schema. Because themes never hold SQLAlchemy objects, a SQLAlchemy upgrade cannot break a third-party theme.

### Fields

Every entity has the core `title` column (display name, always text-searchable, shown on cards). A theme relabels it with `title_label` rather than defining its own name column.

Fields are declared as annotated class attributes: `name: <type> = field(label, *, card=False, search=None, editable=True, detail=True)`. Supported types are `str`, `int`, `float`, `bool`, `date`, and `datetime`; `X | None` makes the column nullable. `field()` returns a plain field spec; the core turns it into a typed column when the keep opens. `search` is one of `"text"`, `"range"` (numbers/dates), `"choice"` (distinct values), or `None`. The core generates cards, detail sections, edit forms, and filter widgets from this metadata. Every entity also has the JSON `extra` column for ad-hoc user fields (searchable by text only).

### Roles

`role(name, *, kinds, many=False, primary=False, thumbnail=False, label=None)`:

- `kinds`: accepted resource kinds (`image`, `audio`, `video`, `font`, `archive`, `dir`, `any`). Drag-and-drop onto a role slot rejects other kinds.
- `many`: one or many resources.
- `primary`: the entity's main content; defines "open file" (§11). At most one primary role per type.
- `thumbnail`: this role is tried early in thumbnail resolution.

### Containment and relationships

- `contains(Parent, Child)` declares containment. The ingester creates edges via `ctx.contain(parent, child)`; the core maintains the closure table.
- `related(name, A, B, *, label=None, reverse_label=None, many=True)` declares a non-containment relationship (Actor ↔ Movie cast). The core builds a link table `(a_id, b_id)`; `many=False` limits each `B` to at most one `A`. The ingester creates links via `ctx.relate(name, a, b)`. `DetailView` sections reference relationships by name, from either side.

### Ingest context

Themes read and write keep data only through the context object `ctx` passed to `ingest()` (and to actions and `migrate()`). Entities are referred to by opaque `EntityRef` handles, not ORM objects. The core applies all writes through the DB writer (§6) and enforces provenance: extracted values never overwrite fields whose provenance is `user`.

- `ctx.upsert(Type, key, *, title=None, **fields) -> EntityRef`: find the entity of that type with this `ingest_key` (§5) or create it, then set extracted fields. The key is the theme's stable natural key (for example, an album's folder path or a normalized artist name).
- `ctx.link(entity, resource, role, sort_order=0)`, `ctx.unlink(...)`.
- `ctx.contain(parent, child)`, `ctx.uncontain(parent, child)`.
- `ctx.relate(name, a, b)`, `ctx.unrelate(name, a, b)`.
- `ctx.find(Type, **equals) -> list[EntityRef]` and `ctx.get(entity) -> Record` (read-only field values) for lookups.
- `ctx.warn(resource, message)`: report a problem with a file to the activity panel.

Batch items are read-only `ResourceInfo` values: root id, relative path, kind, extension, size, mtime, and a readable local path. Themes may open files for reading; they must never write under a root.

### Views

- `SearchView(name, types, *, inherit_tags=False, show_contained=False, layout="grid"|"list"|"tree", default_sort=...)`.
- `DetailView(type, sections=[...])` with sections for fields, role slots, galleries, related entities, and, for containers, an **embedded search** of contents.
- Escape hatch: a view or section may return a custom `QWidget` factory. The core passes it a limited context object (selection, navigation, open-entity callbacks).
- `double_click` per type: `"page"` (default) or `"open_file"`; the other action moves to Ctrl+Enter.

### Actions

`@action(label, applies_to=[types or roles])` registers a context-menu and detail-page action, for example "Play album" (write a temporary `.m3u` in track order and open it with the OS). Actions must not modify user files.

### Theme schema versions

On opening a keep, the core compares the stored theme version with the loaded theme's version: equal → open; older stored → run `migrate()` after backing up `keep.db`; newer stored → refuse to open with a clear message.

### Built-in themes

1. **generic** — one entity per file, no containment. Reference implementation and fallback; lets the core be built and tested before any rich theme exists.
2. **assets2d** — Artist ⊃ Asset. Asset kinds: images, PSD, fonts, archives.
3. **music** — Artist ⊃ Album ⊃ Song. Albums map to directories (role `folder`), songs to files (role `audio`, many for versions). Extraction via mutagen.
4. **movies** — Collection ⊃ Movie, Actor ↔ Movie.

## 10. Thumbnails

### Resolution

Each entity type has an ordered **provider chain** from the theme. Providers return an image source or "no result". Typical chains:

- **Song:** embedded art (ID3 APIC, FLAC picture, MP4 `covr`) → parent Album's thumbnail → generic audio icon.
- **Album:** folder image by convention → embedded art of first track → icon.
- **Actor:** `photo` role → icon.
- **Movie:** `poster` role → first `screenshot` → icon.
- **Archive asset:** first suitable image inside the archive → icon.

Built-in providers: `RoleImage(role)`, `FolderImage(names)`, `EmbeddedAudioArt()`, `ArchiveFirstImage()`, `ParentThumbnail()`, `ImageFile()`, `Icon(name)`.

### Folder conventions

Case-insensitive, any of `.jpg .jpeg .png .webp`: `folder`, `cover`, `front`, `albumart`, `AlbumArt_*_Large`, `AlbumArtSmall`. Themes can extend the list.

### Archives

- List members; skip directories, `__MACOSX/`, dotfiles, and non-images.
- Prefer names containing `cover`, `preview`, `thumb`; otherwise the first image in natural sort order.
- Read only the chosen member. Zip supports random access; solid 7z/RAR may need to decompress preceding data, so archive thumbnails always run in workers with a size/time budget and give up gracefully.

### Cache

- `thumbs.db` table `thumb(key text pk, size int, format text, data blob, created_at)`.
- `key = hash(resource_id, size, mtime, provider_id, provider_version, thumb_size)`, so changes and provider upgrades invalidate automatically.
- Stored as WebP (fallback PNG). The entire file can be deleted at any time.
- The resolved source resource is memoized in `entity.thumb_resource_id` to avoid re-running the chain.

## 11. Opening files

- **Default:** hand the file to the OS via `QDesktopServices.openUrl(QUrl.fromLocalFile(...))`. This supports UNC and mapped paths.
- **Always available:** "Reveal in file manager" and "Open with…" (one-time choice).
- **Overrides:** optional per-user rules keyed by extension (and optionally role) with a command template, for example `"C:\Tools\viewer.exe" "{path}"`. Placeholders: `{path}`, `{dir}`, `{name}`. Stored in per-user settings, not the keep.
- **Archive members** (when supported): extract the single member to a temp directory, then open it.
- **Gestures:** single click selects; double-click opens the entity page (or the primary file if the type's `double_click="open_file"`); Ctrl+Enter does the other. Inside a detail page's role sections, double-clicking a file opens it.
- Tagalot never writes, moves, renames, or deletes files under a root.

## 12. User interface

### Main window layout

Three panes:

- **Left: navigation.** Dashboard, global search, the theme's scoped searches, saved searches, triage, dedupe, tag manager.
- **Center:** the current view (results, detail page, or manager screen).
- **Right: tagging panel** (dockable, visible on search and detail views).
- **Status bar:** activity indicator opening the activity panel.

Keep configuration and the keep launcher are separate windows/dialogs.

### Navigation model

- **Select** (single click): updates the tagging panel and an optional preview strip (thumbnail + key fields).
- **Open** (double-click/Enter): navigates to the entity's detail page.
- **Drill down** ("Show contents in search"): adds a removable `Within: <entity>` chip to the current search, keeping current tag filters.
- **Back/forward** history like a browser (Alt+←/→, mouse buttons).
- **Breadcrumbs** from the containment hierarchy (Artist › Album › Song); each crumb opens that container's page.

### Views

**Keep launcher.** Recent keeps, create keep (name, location, theme, first root), open folder. Shows theme problems: theme missing, stored version newer or older (offer migration with backup).

**Keep configuration.** Roots: add, remove, rename, change path, per-machine override, exclude patterns, status (online, item count, last scan, last error), "Scan now". Also thumbnail size, clear thumbnail cache, theme info.

**Dashboard.** Opening screen for a keep: counts per entity type, recently added items, untagged share (links to triage), root health, most/least used tags. Themes can add cards (for example, total runtime).

**Search view.** Filter bar with include chips, exclude chips ("but not"), field filters (only valid ones for the scope), `Within` chip, text box, toggles for "Show contained items" and "Inherit tags" (plus advanced "Match via contents"). Results as grid (thumbnails), list (columns), or tree/grouped. Multi-select supported.

**Detail page.** Theme-declared sections: fields (inline edit; user edits set provenance `user`), role slots (drop files to link), galleries, related entities, and for containers an embedded search of contents with its own filter bar. Header shows breadcrumbs and actions.

**Tagging panel.**
- Filter box at the top: narrows the tree as you type, keeping ancestors of matches visible; matches aliases.
- Drag one or more tags onto an item or selection.
- Keyboard: type to filter, arrows to move, Enter applies the highlighted tag to the selection, Shift+Enter removes it.
- Selection summary: each tag shows a tri-state check (all / some / none of the selected items). Clicking toggles between applying to all and removing from all.
- "Create tag '…'" appears when the filter text matches nothing.

**Tag manager.** Full tree with usage counts; add, rename, reparent (drag or "Move to…"), merge, delete with subtree options, aliases, colors. Undo within the session.

**Triage.** Tabs: resources not linked to any entity; entities with no tags; entities whose linked resources are all missing. Bulk actions: link to entity/role, tag, dismiss (hide until changed).

**Dedupe.** Groups of exact duplicates (fingerprint) and theme-suggested near-duplicates. Side-by-side comparison of files and entities (format, size, resolution/bitrate, tags). Actions: merge entities (§13), keep both files as versions under one entity, or mark "not a duplicate." Never deletes files.

**Activity panel.** Scan progress per root, fingerprint and thumbnail queues, offline roots, ingest and thumbnail errors with the affected paths.

## 13. Dedupe and entity merge

- **Exact duplicates:** resources with equal fingerprints (optionally verified with a full hash).
- **Near-duplicates:** the theme's `similarity(a, b)` hook scores candidate pairs within a type (music: normalized artist + title + duration; images: a perceptual hash). The core only compares candidates produced by cheap blocking keys the theme supplies, never all pairs.
- **Entity merge:** the kept entity absorbs the other's tags, role links, containment edges, relationships, and `user`-provenance field values (conflicts are shown for the user to choose). The merged entity id is recorded so saved searches and history can redirect. Files are never touched.
- "Keep both as versions" links the second file to the kept entity under the same primary role.

## 14. Future work

- **Archive members as resources**, using `resource.parent_resource_id` and paths inside the archive, so a font inside a zip can be its own entity.
- **Metadata fetchers** (TMDb, MusicBrainz) as an optional theme hook writing fields with provenance `fetched`.
- **Rust hot spots** via PyO3 if profiling justifies them.
- **Entry-point theme packaging** and a theme template/cookiecutter.
- Scheduled background rescans.

## 15. Open questions

1. Should `aggregate_up` be exposed in the main filter bar or only in an advanced menu?
2. How should entity merges interact with tag inheritance when the two entities have different parents?
3. Is there a need for tags scoped to one entity type, or are all tags global to the keep?
4. Packaging: PyInstaller vs. Briefcase vs. Nuitka for distributable builds.

## 16. Decisions log

| Date | Decision |
|---|---|
| 2026-09 | Python + PySide6 + SQLAlchemy 2.0 + SQLite; Rust only for isolated hot spots. |
| 2026-09 | Tags attach to entities, not files. Entities link to resources through roles. |
| 2026-09 | Containment is a DAG with a core-maintained closure table. |
| 2026-09 | Tag inheritance and "show contained items" are per-view defaults with user toggles. |
| 2026-09 | Themes are single Python files importing only `tagalot.themes.api`. |
| 2026-09 | Files open via the OS default handler; per-user overrides by extension. |
| 2026-09 | The app never modifies, moves, renames, or deletes user files. |
| 2026-09 | Tag sibling-name uniqueness is enforced by a unique index on `(coalesce(parent_id, 0), lower(name))`, plus a `casefold()` check in `tags.py` for clear errors. A plain `(parent_id, lower(name))` constraint would not cover root-level tags, because SQLite treats NULLs as distinct. |
| 2026-09 | Closure maintenance recomputes the ancestors of affected nodes (descendants of each changed edge's child) from `entity_contains` with a recursive CTE, for both additions and removals. Path counting was rejected because it cannot maintain minimum depth. `rebuild_all()` serves as the repair action and test oracle (§6). |
| 2026-09 | Theme entity classes are plain declarations (annotated fields, no SQLAlchemy). When a keep opens, the core builds the theme's tables as Core `Table` objects in a fresh `MetaData`. Themes access data only through `ctx` with `EntityRef` handles, and relationships are declared with `related()`. This isolates themes (a broken theme affects only its keeps), allows reloading, and keeps SQLAlchemy out of the public API. Rejected: theme classes as ORM subclasses in a shared registry, because mapping at import lets one broken theme break every keep. Added `entity.ingest_key` for idempotent re-ingest (§5, §9). |
| 2026-09 | keep.toml: strings are written as TOML literal strings where possible so Windows and UNC paths stay hand-editable; root paths are stored as raw strings; unknown keys are ignored with a warning and dropped on rewrite; writes are atomic (§4). |
| 2026-09 | Per-user settings live in a non-roaming config dir and never block startup: unparseable files are set aside as `settings.toml.invalid`, bad entries are skipped with a warning (§4). |
| 2026-09 | Keep creation requires a new or empty folder and refuses a keep and root that contain each other. Network detection for the keep-location warning uses UNC/`GetDriveTypeW` on Windows and mount file-system types on Linux/macOS, and fails open (§4). |
| 2026-09 | Network keeps use `journal_mode=DELETE` instead of WAL. Read-only engines set `query_only`. The pysqlite driver's own transaction handling is disabled so DDL runs inside transactions (§4). |
| 2026-09 | Core schema conventions: `resource.mtime_ns` as integer nanoseconds; UTC-aware timestamps; enum columns as text with CHECK constraints; link rows cascade on delete, while deleting a tag with children or a root with resources is refused (§5). |
| 2026-09 | Core schema versions: a newer keep is refused; an older one needs user confirmation, then is backed up with SQLite's backup API and migrated in a single transaction; `keep.toml` `format_version` tracks the database (§5). |
| 2026-09 | Root reachability: a root is online if its folder can be listed within 10 s, checked on a daemon thread so a hung share can be abandoned; offline marks only `ok` resources `offline` (§6). |
| 2026-09 | Walking: exclude globs are case-insensitive with `**` semantics and prune matching folders; symlinked folders and junctions are not followed; read errors are reported and skipped (§6). |
| 2026-09 | Scan diff: resources under unreadable folders are not marked missing; returning files keep their id; `last_seen_at` moves only for resources actually seen. Newly excluded resources currently become `missing` (issue #152) (§6). |
| 2026-09 | Fingerprints are 16-byte blake2b digests; files up to 128 KiB are hashed whole; results are stored only if size and mtime still match (§5). |
| 2026-09 | Move detection matches unlinked new files to missing files by fingerprint across roots with no time window; ambiguous groups pair only by unique file name, never by guess (§6). |
| 2026-09 | The DB writer is a dedicated thread applying `Callable[[Connection], T]` jobs in their own transactions and returning futures; scans are applied in parts of at most 500 changes (§6). |
| 2026-09 | `scan_root()` orchestrates §6 steps 1–4 per root: one fingerprint pass per scan, move detection limited to the scan's new files, and a `ScanReport` for the UI (§6). |
| 2026-09 | The tag tree is cached as an immutable snapshot, reloaded only after explicit invalidation by tag operations; tag names compare with `str.casefold()` (§7). |
| 2026-09 | Tag merge merges clashing children recursively and is refused into a descendant; promoting children on delete is refused on name clashes; operations validate against a fresh tree inside their transaction (§7). |
| 2026-09 | Tag undo/redo restores snapshots of the tag tables plus the operation's own `entity_tag` delta instead of computing inverse operations (§7). |
| 2026-09 | `SearchSpec` is frozen and uses tuples (hashable); field filters are `TextFilter` (contains or starts-with)/`RangeFilter`/`ChoiceFilter`; its JSON is versioned with tagged dates (§8). |
| 2026-09 | Search: a deleted tag in a spec is a subtree of itself (include matches nothing, exclude excludes nothing); field names resolve through a column mapping (core: title/created_at/updated_at); results always tie-break on entity id (§8). |
| 2026-09 | Closure API: `apply` (removals first, ordered cycle checks, returns rejections), `add_entities` for self rows, and `detach` before deleting entities; affected ids use a temporary table (§6). |
| 2026-09 | Containment search: `within` is strict; `aggregate_up` matches tags and text via non-excluded descendants while field filters and exclusion apply to the container; `show_contained` adds descendants regardless of types, subject only to exclusion (§8). |
| 2026-09 | Search performance: set tests are uncorrelated `IN` subqueries over materialized CTEs; closure cycle checks run only for edges into entities that have children. Measured at 50.5k entities: 1–47 ms for single options, ~105 ms with every option at once (§6, §8). |
| 2026-09 | Text search uses an FTS5 table with the trigram tokenizer (substring matching, case- and diacritic-insensitive) kept in sync by the DB writer. A word-based tokenizer was rejected because it cannot match inside words ("bey" would not find "Abbey"). The roughly 5× larger index (about 20 MB per 50k entities) is acceptable (§8). |
