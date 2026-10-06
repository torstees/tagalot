# Keep files and settings

## keep.toml

Each keep's `keep.toml` says what it is. Tagalot writes it as you change things in Keep configuration; it's also fine to edit by hand (with the keep closed). Paths are written as literal strings, so Windows and UNC paths need no escaping.

```toml
[keep]
id = "0b6e3c1e-6f0a-4b54-9a8e-2b2d7f1c9d41"   # never changes
name = "Music"
format_version = 14                         # the keep's format (see Upgrading)

[theme]
id = "music"
version = 1                                 # the theme's own version

[theme.options]                             # the theme's options, for this keep
# artist_level = 2

[[roots]]                                   # a watched folder; one table per folder
id = "nas-music"                            # never changes; items refer to it
name = "NAS music share"
path = '\\nas\music'                        # exactly as written
exclude = [                                 # Skip patterns
    '**/.DS_Store',
    '**/Thumbs.db',
    'Old/**',
]
# options = { artist_level = 1 }            # the theme's options, for this folder only
# watched = false                           # not scanned; its items stay, offline
# writable = true                           # Write to file… may change files here

[roots.exclude_notes]                       # why a Skip pattern is there
'Old/**' = 'not sorted yet'

[thumbnails]                                # optional
max_size = 512                              # the size thumbnails are made at
after_scan = false                          # don't make a scan's thumbnails in the background

[online]                                    # once you've answered the question
lookups = "allow"                           # or "never"

[contents]                                  # searching inside documents
index = "words"                             # or "substrings"; absent: Off
```

Unknown keys are ignored (with a warning in the log), so an older Tagalot can still open a keep a newer one wrote, as long as its format is one it knows.

### Skip patterns

Matched against each file's path within its folder, with `/` between folders, ignoring case:

| Pattern | Matches |
|---|---|
| `*.tmp` | `.tmp` files directly in the folder |
| `**/*.tmp` | `.tmp` files anywhere |
| `**/Extras/**` | everything in any `Extras` folder |
| `Old/**` | everything in the top-level `Old` folder |
| `Notes/draft?.md` | `draft1.md`, `draftA.md`… in `Notes` |

`*` matches within one folder name, `**` across folders, `?` one character, and `[abc]` one of those characters.

## settings.toml

Your own settings, for this computer, not in any keep:

| | |
|---|---|
| Windows | `%LOCALAPPDATA%\tagalot\settings.toml` |
| macOS | `~/Library/Application Support/tagalot/settings.toml` |
| Linux | `~/.config/tagalot/settings.toml` |

It holds:

- **recent keeps** (the launcher's list, at most 10);
- **On this computer** paths: `[root_overrides.<keep id>]`, a folder id → this computer's path;
- **programs for opening files** (Open with → Always open .ext files with…): `[[handlers]]` tables with `ext`, an optional `role`, and a `command` such as `"C:\Tools\viewer.exe" "{path}"` (placeholders `{path}`, `{dir}`, and `{name}`);
- **extra themes folders** (`theme_dirs`);
- the folder last chosen in a folder picker.

A missing file means the defaults; one that can't be read is renamed `settings.toml.invalid` and the defaults are used.

## ui_state.json

In each keep's folder: how you left its pages. Per search: layouts, hidden and shown columns, card lines, and toggles; and for the keep: the thumbnail size, whether the preview strip shows, and which navigation headings are folded. Deleting it resets them.

## Themes folder

Where Tagalot looks for themes of your own (see [the theme guide](../THEMES.md)):

| | |
|---|---|
| Windows | `%LOCALAPPDATA%\tagalot\themes` |
| macOS | `~/Library/Application Support/tagalot/themes` |
| Linux | `~/.local/share/tagalot/themes` |

## Upgrading

A keep records its format (`format_version`). A newer Tagalot that needs a newer format asks before upgrading a keep, and first copies `keep.db` to `keep.db.v<old format>-<date and time>.bak` beside it. An older Tagalot can't open a keep a newer one upgraded: keep the backup if you might go back. A theme whose version changed may read every file again once after the upgrade.
