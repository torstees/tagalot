# Activity and checks

## The Activity panel

**View → Activity** (Ctrl+Shift+A), or the **⚠ N** badge at the right of the status bar, opens a panel under the results (move or float it as you like). It never opens by itself.

- **Now:** what the scan is doing, then a summary of the last one ("Last scan, 14:02: …").
- **Thumbnails:** how many are left to make in the background.
- **Folders:** each folder's state: online, offline (and why), not watched, or not scanned yet.
- **Problems:** newest first, what went wrong, where, and why:

  | | |
  |---|---|
  | **Can't read** | a folder or file couldn't be read (permissions, a damaged file) |
  | **Not added** | the theme couldn't make an item from a file |
  | **Warning** | the theme noticed something about a file |
  | **No thumbnail** | a file couldn't be drawn |
  | **Offline** | a folder couldn't be reached |
  | **Scan failed** | a scan stopped |
  | **Lookup** | an [online lookup](online-details.md) didn't work |
  | **Text not read** | a document's text couldn't be read |

  **Show in file manager** (or double-click) shows a problem's file; **Copy** puts the list on the clipboard; **Clear** empties it.

A problem with one file never stops a scan; the rest are read as usual. The list lasts while the keep is open (the newest 1,000).

## tagalot --check

```bash
tagalot --check
```

reports what this copy of Tagalot can do, without opening a window, one line each with **ok** or what's wrong:

- the version, Python, Qt, and SQLite's text search (FTS5);
- the themes it found (and any theme file that failed to load, with why), and the template for new themes;
- the icon;
- the libraries for reading files: Pillow (pictures), mutagen (music tags), MediaInfo (video details), PDFium (PDFs), PyYAML and ruamel.yaml (Markdown front matter), py7zr (7z archives), and RAR (and whether a tool to read RAR archives is installed).

```
Tagalot 0.7.0
Python         ok           3.12.11 (win32, AMD64)
Qt             ok           PySide6 6.11.2, Qt 6.11.2
SQLite FTS5    ok           available
Themes         ok           assets2d, books, generic, movies, music, research
…
RAR            ok           rarfile 4.5 (with an unrar tool)
All good.
```

It exits with an error when something is missing. From source, run `uv run tagalot --check`; packaged Windows builds have no console, so add `--output check.txt` and read the file.

For theme authors: `tagalot --check-theme <id or file>` checks one theme ([the theme guide](../THEMES.md)).
