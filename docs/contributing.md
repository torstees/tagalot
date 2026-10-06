# Working on Tagalot

Tagalot is Python (3.12+, managed with [uv](https://docs.astral.sh/uv/)), with PySide6 for the window and SQLAlchemy on SQLite.

```bash
uv sync                 # install, with the development tools
uv run tagalot          # run it
uv run pytest           # the tests (GUI tests run headless)
uv run ruff check .     # lint
uv run ruff format .    # format
uv run mypy src         # type-check
```

or, with [just](https://just.systems): `just check` runs every check, `just fix` lints and formats, and `just demo` (and `just demo-music`, `just demo-books`…) makes and opens a [demo keep](getting-started/demo-keeps.md).

## Where things are

- **[AGENTS.md](https://github.com/torstees/tagalot/blob/main/AGENTS.md):** how work is done: the workflow (issues, branches, pull requests), the rules no change may break (never change a user's files; the core never imports Qt; themes import only the theme API…), conventions, and commands. Read it first.
- **[DESIGN.md](DESIGN.md):** the design, the source of truth for the data model, search, the theme API, and the window, with a log of every decision.
- **[THEMES.md](THEMES.md):** the guide for writing themes.
- **The work plan:** [GitHub issues](https://github.com/torstees/tagalot/issues), grouped into milestones (each an issue labelled `epic`) on the [project board](https://github.com/users/torstees/projects/5).

## These docs

The site is [docsify](https://docsify.js.org): Markdown in `docs/`, no build step. Preview it with:

```bash
just docs               # or: uv run python -m http.server 3000 --directory docs
```

then open <http://localhost:3000>. The sidebar is `docs/_sidebar.md`; the cover is `docs/_coverpage.md`. Pages link to each other relatively, so they read on GitHub too.

**Screenshots** (`docs/images/screens/`) come from the demo keeps, at one window size:

```bash
just screenshots        # or: uv run python scripts/make_screenshots.py
```

**The pictures** (logo, knight, flag, favicon in `docs/images/`) are rendered from the SVGs in `src/tagalot/resources` by `just icons`.

When a change alters what people see or do, update the pages here in the same pull request.
