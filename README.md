# Tagalot

A tag-based file browser. Point a **keep** at folders of music, movies, 2D art, or anything else; Tagalot scans them (network shares included) into items you can tag in a hierarchy, search, and browse, without ever touching your files. A **theme** decides what the items are: songs and albums, movies and their cast, images and fonts, or plain files.

## Get it

- **Download** from [Releases](https://github.com/torstees/tagalot/releases):
  - **Windows:** run `Tagalot-…-Windows-setup.exe`. It installs for you alone (no administrator needed) and adds Tagalot to the Start menu.
  - **macOS:** open the `.dmg` and drag Tagalot to Applications.
  - **Linux:** make the `.AppImage` executable (`chmod +x`) and run it.
  - **Without installing:** the `.zip` (`.tar.gz` on Linux) holds the same program. Unzip it, keep the folder together, and run `tagalot.exe` (Windows), `Tagalot.app` (macOS), or `tagalot` (Linux) inside it.
- **The builds aren't signed yet,** so the first run asks:
  - **Windows:** "Windows protected your PC": choose **More info → Run anyway**.
  - **macOS:** it says Tagalot can't be opened: go to **System Settings → Privacy & Security** and choose **Open Anyway**.
- **From source** (Python 3.12+ and [uv](https://docs.astral.sh/uv/)): `uv sync`, then `uv run tagalot`.

## Docs

- [Writing a theme](docs/THEMES.md): make Tagalot understand your kind of files.
- [Design](docs/DESIGN.md): how Tagalot works, and why.
- [AGENTS.md](AGENTS.md): working on Tagalot itself (commands, conventions, the demo keeps).
