# Install

## From a release

Download the latest from [GitHub Releases](https://github.com/torstees/tagalot/releases):

| | |
|---|---|
| **Windows** | Run `Tagalot-…-Windows-setup.exe`. It installs for you alone (no administrator needed) and adds Tagalot to the Start menu. |
| **macOS** | Open the `.dmg` and drag Tagalot to Applications. |
| **Linux** | Make the `.AppImage` executable (`chmod +x Tagalot-*.AppImage`) and run it. |

**Without installing:** the `.zip` (`.tar.gz` on Linux) holds the same program. Unzip it, keep the folder together, and run `tagalot.exe` (Windows), `Tagalot.app` (macOS), or `tagalot` (Linux) inside it.

### The first run

The builds aren't signed yet, so the first run asks:

- **Windows:** "Windows protected your PC": choose **More info → Run anyway**.
- **macOS:** it says Tagalot can't be opened: go to **System Settings → Privacy & Security** and choose **Open Anyway**.

## From source

With Python 3.12 or later and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/torstees/tagalot.git
cd tagalot
uv sync
uv run tagalot
```

## Checking what your copy can do

```bash
tagalot --check
```

lists what this copy of Tagalot can do: the themes it found (and any that failed to load), and the libraries for reading files (PDFs, video details, archives, and whether a tool for RAR archives is installed). From source, run `uv run tagalot --check`. Packaged Windows builds have no console: add `--output check.txt` and read the file. See [Activity and checks](../guide/activity.md).

## Opening a keep directly

```bash
tagalot path/to/Music.keep
```

opens that keep instead of the launcher.

Next: [your first keep](first-keep.md).
