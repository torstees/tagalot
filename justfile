# Shortcuts for common Tagalot tasks. Run `just` to list them.
# Install just once with `uv tool install rust-just` (or `winget install Casey.Just`).
# Every recipe is a plain `uv run …` command, so nothing here is required.

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# List the recipes
default:
    @just --list --unsorted

# Install dependencies, including dev tools
sync:
    uv sync

# Launch Tagalot, optionally with a keep folder: just run scratch/Demo.keep
run *args:
    uv run tagalot {{ args }}

# (Re)create the demo keep and open it
demo: demo-reset
    uv run tagalot scratch/Demo.keep

# (Re)create Media.keep (artists, albums, songs) and open it; Demo.keep is left alone
demo-media:
    uv run python scripts/make_demo_keep.py --reset --media
    uv run tagalot scratch/Media.keep

# (Re)create Assets.keep (the 2D assets theme) and open it; Demo.keep is left alone
demo-assets:
    uv run python scripts/make_demo_keep.py --reset --assets
    uv run tagalot scratch/Assets.keep

# (Re)create Research.keep (the research theme) and open it; Demo.keep is left alone
demo-research:
    uv run python scripts/make_demo_keep.py --reset --research
    uv run tagalot scratch/Research.keep

# (Re)create Books.keep (the books theme) and open it; Demo.keep is left alone
demo-books:
    uv run python scripts/make_demo_keep.py --reset --books
    uv run tagalot scratch/Books.keep

# (Re)create Movies.keep (the movies theme) and open it; Demo.keep is left alone
demo-movies:
    uv run python scripts/make_demo_keep.py --reset --movies
    uv run tagalot scratch/Movies.keep

# (Re)create Music.keep (the music theme) and open it; Demo.keep is left alone
demo-music:
    uv run python scripts/make_demo_keep.py --reset --music
    uv run tagalot scratch/Music.keep

# (Re)create scratch/Demo.keep and scratch/demo-files without opening them
demo-reset:
    uv run python scripts/make_demo_keep.py --reset

# Run the tests; extra arguments go to pytest: just test tests/ui -k launcher
test *args:
    uv run pytest {{ args }}

# Run the tests without the GUI tests and benchmarks
test-fast *args:
    uv run pytest -m "not gui and not slow" {{ args }}

# Fix lint problems and format the code
fix:
    uv run ruff check . --fix
    uv run ruff format .

# Everything a change must pass before it's done (as CI runs it, plus typing the tests)
check:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy src
    uv run mypy tests
    uv run pytest

# Build the app with PyInstaller into dist/ (dist/Tagalot.app on macOS) and check it
package:
    uv sync --group package
    uv run pyinstaller packaging/tagalot.spec --noconfirm
    uv run python scripts/check_package.py

# Build the app, then this OS's installer into dist/ (setup .exe, .dmg, or AppImage)
installer: package
    bash packaging/installer.sh

# Render the icon (src/tagalot/resources/tagalot-shield-hash.svg) to its PNGs, .ico, and
# .icns, and the docs site's favicon and pictures to docs/images
icons:
    uv run python scripts/make_icons.py

# Serve the documentation site (docs/, docsify) at http://localhost:3000
docs:
    uv run python -m http.server 3000 --directory docs

# Retake the documentation site's screenshots (docs/images/screens) from the demo keeps
screenshots:
    uv run python scripts/make_screenshots.py

# Time scanning and thumbnailing a folder or share: just profile PATH [--theme generic] [--cprofile]
profile path *args:
    uv run python scripts/profile_scan.py "{{path}}" {{args}}
