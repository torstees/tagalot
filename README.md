# Tagalot

A tag-based file browser. Point a **keep** at folders of music, movies, 2D art, or anything else; Tagalot scans them (network shares included) into items you can tag in a hierarchy, search, and browse, without ever touching your files. A **theme** decides what the items are: songs and albums, movies and their cast, images and fonts, or plain files.

## Get it

- **Download** a build for Windows, macOS, or Linux from [Releases](https://github.com/torstees/tagalot/releases). Unzip it, keep the folder together, and run `tagalot.exe` (Windows), `Tagalot.app` (macOS), or `tagalot` (Linux) inside it. The builds aren't signed yet, so Windows and macOS ask before running them.
- **From source** (Python 3.12+ and [uv](https://docs.astral.sh/uv/)): `uv sync`, then `uv run tagalot`.

## Docs

- [Writing a theme](docs/THEMES.md): make Tagalot understand your kind of files.
- [Design](docs/DESIGN.md): how Tagalot works, and why.
- [AGENTS.md](AGENTS.md): working on Tagalot itself (commands, conventions, the demo keeps).
