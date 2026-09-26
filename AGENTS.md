# AGENTS.md

Guidance for AI coding agents (and humans) working on Tagalot.

## What this project is

Tagalot is a Python desktop app: a tag-based file browser organized into **keeps**. Each keep has its own SQLite database, hierarchical tag tree, watched directories ("roots", often network shares), and a pluggable **theme** (music, 2D assets, movies…) that defines entity types, metadata fields, views, and thumbnails.

**Read `docs/DESIGN.md` before making any architectural change.** It is the source of truth for the data model, search semantics, theme API, and UI. The work plan lives in GitHub issues on `torstees/tagalot`, tracked in the [Tagalot project](https://github.com/users/torstees/projects/5): each milestone (M0–M18) is a parent issue labelled `epic`, and each task is a sub-issue of it, listed in the order to work them.

## Workflow

1. Pick the first open sub-issue of the earliest open milestone issue unless told otherwise (`gh issue list --label epic` lists milestones; `gh api repos/torstees/tagalot/issues/<n>/sub_issues` lists a milestone's tasks in order).
2. Read the relevant sections of `docs/DESIGN.md`.
3. Implement with tests. Run the full check suite (below) before finishing.
4. Close completed issues from the commit or PR (`Closes #<n>`). File newly discovered work as a new issue and add it as a sub-issue of the right milestone. Close a milestone issue when all its sub-issues are closed.
5. If you deviate from the design or make a decision it doesn't cover, update `docs/DESIGN.md` (the relevant section and the Decisions log) in the same change. Don't let code and design drift apart.
6. Keep changes focused: one milestone item (or a coherent part of one) per change.
7. Land every change through a branch and pull request, never a direct commit to `main` (see Pull requests below).

## Pull requests

- Branch from an up-to-date `main`, named `<milestone>/<topic>` (for example `m1/keep-toml`), or `docs/<topic>` for documentation-only changes.
- Reference the issue in the commit and PR body with `Closes #<n>` so merging closes it.
- The PR body has these sections:
  - **What:** the changes, and any decision made along the way (with the `docs/DESIGN.md` update, if one was needed).
  - **Checks:** the automated checks that were run and their results.
  - **Manual testing:** a checklist for the reviewer, using `- [ ]` checkboxes, whenever the change has behavior a person can try: anything in the UI, launching the app, scanning real folders or network shares, opening files. Each item gives the steps and the expected result, for example `- [ ] Run uv run tagalot; an empty main window titled "Tagalot" opens`. Cover the platform-specific cases the change touches (Windows paths, UNC shares, an offline root). When there is nothing to try by hand, write "None: covered by automated tests."

## Commands

```bash
uv sync                          # install dependencies (including dev)
uv run tagalot                   # launch the app
uv run pytest                    # all tests
uv run pytest -m "not gui"       # non-GUI tests only
uv run ruff check . --fix        # lint
uv run ruff format .             # format
uv run mypy src                  # type-check
```

GUI tests use pytest-qt and must run headless: `QT_QPA_PLATFORM=offscreen` is set in the pytest config. All four checks (pytest, ruff check, ruff format --check, mypy) must pass before a task is done.

## Stack

- Python 3.12+, managed with **uv**. `src/` layout.
- **PySide6** for the UI. **SQLAlchemy 2.0** (typed `Mapped[...]`, `select()` style; no legacy `Query` API) on **SQLite**.
- Pillow, mutagen, py7zr, rarfile (optional at runtime), platformdirs, tomli-w.
- pytest, pytest-qt, ruff, mypy.

Do not add a dependency without a clear need. When you add one, note it and the reason in `docs/DESIGN.md` §3.

## Repository layout

```
src/tagalot/
  __main__.py            # entry point
  core/                  # no Qt imports allowed here
    keep.py              # open/create keep, keep.toml
    settings.py          # per-user settings.toml (recent keeps, overrides)
    tomlio.py            # hand-editable TOML output, atomic writes
    db.py                # engine/session setup, WAL, schema versioning
    models.py            # core tables (resource, entity, tag, links, closure…)
    scanner.py           # walking, diffing, move detection
    fingerprint.py
    ingest.py            # drives theme ingesters, DB writer batches
    closure.py           # entity_ancestor maintenance
    tags.py              # tag tree ops: add/rename/reparent/merge/delete
    search.py            # SearchSpec -> SQLAlchemy query
    thumbnails/          # providers, cache, archive reader
    handlers.py          # open file / reveal / overrides
    dedupe.py
  themes/
    api.py               # PUBLIC theme API — the only module themes import
    loader.py            # discovery and validation
  builtin_themes/        # generic.py, assets2d.py, music.py, movies.py
  ui/                    # all Qt code
    app.py, main_window.py, launcher.py, keep_config.py,
    search_view.py, detail_view.py, tag_panel.py, tag_manager.py,
    triage.py, dedupe_view.py, dashboard.py, activity.py,
    models/              # QAbstractItemModel subclasses
    workers.py           # QThreadPool jobs, signals
tests/
  fixtures/              # tiny sample files (images, zips, audio)
  core/, themes/, ui/
docs/DESIGN.md
```

## Architecture rules (do not break these)

1. **Never modify user files.** Tagalot must never write, move, rename, or delete anything under a root. It writes only inside the keep folder, the per-user config/cache directories, and the system temp directory. Any code path that could touch a root with a write operation is a bug.
2. **`core` never imports Qt or `ui`.** Core logic must be testable without a display. The UI depends on core, never the reverse.
3. **Themes import only `tagalot.themes.api`.** Treat `api.py` as a public, versioned contract. Changes to it need a design-doc update. Built-in themes follow the same rule as third-party ones.
4. **The GUI thread never blocks on I/O.** File-system walks, network access, hashing, metadata extraction, archive reading, image decoding, and slow queries run in workers and report back through signals.
5. **One DB writer.** Workers send result batches to the writer; they don't open write transactions themselves.
6. **Paths in the database are root id + relative POSIX path.** Never store absolute paths. Use `pathlib`; convert at the boundary. Test with Windows-style and UNC paths in mind (`PureWindowsPath`).
7. **Offline ≠ missing ≠ deleted.** An unreachable root marks resources offline. Never delete resource or entity rows because a root was unreachable.
8. **Tags attach to entities, not resources.** Parent tags are never stored implicitly; hierarchy is expanded at query time.
9. **Respect field provenance.** Extraction never overwrites a field the user edited.
10. **Build SQL with SQLAlchemy expressions.** No string-concatenated SQL.

## Code conventions

- Full type hints; mypy must pass. Prefer `dataclass`es for value objects (e.g., `SearchSpec`).
- Small, focused modules. Docstrings on public functions and on everything in `themes/api.py`.
- Errors from bad files (corrupt archives, unreadable tags, permission errors) are caught, logged with the path, surfaced in the activity panel, and never crash a scan.
- Use `logging` (module-level loggers), not `print`.
- Qt: use model/view classes for lists and trees with many rows; don't populate `QListWidget`/`QTreeWidget` with thousands of items.

## Testing

- Core logic gets unit tests against a real SQLite database in `tmp_path`. Don't mock SQLAlchemy.
- Scanner tests build directory trees in `tmp_path`. Simulate an offline root by pointing it at a nonexistent path.
- Keep fixture files tiny (a few KB). Generate images and archives in tests where possible (Pillow, `zipfile`). Commit small audio fixtures with known tags under `tests/fixtures/` along with a README noting their contents.
- Search semantics (include/exclude, descendant expansion, inheritance, show-contained, within) need thorough table-driven tests; they are the heart of the app.
- Mark GUI tests with `@pytest.mark.gui`.
- For performance-sensitive code (search, scanning), add a benchmark test that builds ~50k entities and asserts a generous time bound; mark it `@pytest.mark.slow`.

## When unsure

Prefer the simplest thing consistent with `docs/DESIGN.md`. If the design is silent or ambiguous on something that affects the data model or the theme API, stop and ask rather than guessing; for smaller choices, decide, implement, and record the decision in the Decisions log.
