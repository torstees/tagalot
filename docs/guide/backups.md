# Backups

Your tags, notes, and everything else you did by hand live in the keep's `keep.db`. Your files are never changed, but the keep is worth backing up, ideally on a schedule.

## tagalot --backup

```bash
tagalot --backup "D:\Keeps\Movies.keep" --to "E:\Backups\Tagalot" --keep-last 14
```

makes a complete copy of the keep: `keep.db` and `keep.toml`, in a dated folder:

```
E:\Backups\Tagalot\
  backup.log
  Movies\
    2026-10-30_0300\
      keep.db
      keep.toml
```

- **It's safe while Tagalot is open:** the copy is one consistent moment of the keep, even while you're tagging.
- **Each copy is checked.** A damaged copy is renamed `…_damaged`, and the command fails.
- **`--keep-last N`** deletes the oldest copies beyond N. Damaged copies don't count, so a bad night never deletes your older good copies. Without it, nothing is deleted.
- **Each keep has its own folder** under `--to` (its name without `.keep`), so several keeps can share one backup folder.
- **`backup.log`** gets one line per run:

  ```
  2026-10-30 03:00:02  ok  Movies.keep -> 2026-10-30_0300 (12.4 MB), deleted 1 older
  2026-10-31 03:00:02  FAILED  Movies.keep: damaged: …
  ```

Thumbnails and the text read from documents aren't copied: Tagalot makes them again as needed.

From source, run `uv run tagalot --backup …`. Packaged Windows builds have no console, so add `--output backup.txt` to read what it printed. The log is written either way.

## tagalot --export-tags

```bash
tagalot --export-tags "D:\Keeps\Movies.keep" --to "E:\Backups\Tagalot\Movies tags.yaml" --items
```

writes the same file as **Tag manager → Export tags…**, which **Import tags…** loads back ([Tags](tags.md)). A backup of `keep.db` is complete, but this file is one you can read and edit:

- **Without `--items`:** the tag tree (**Tags only…**).
- **With `--items`:** the tree, then every item with tags of your own (**Tags and the items that have them…**).
- **Pictures:** the tags' pictures and icons go in a folder beside it (`Movies tags pictures`).
- **Replacing:** each run replaces the file, and a run that fails leaves the last one whole.
- **The keep isn't changed,** and it can be open in Tagalot meanwhile. A keep that needs upgrading after you updated Tagalot isn't exported: open it in Tagalot once.
- **Watched folders:** saving inside a folder the keep watches is refused, since no one is there to confirm as the window asks. Add `--allow-watched` to save there anyway. Even then, a file already there is never replaced, and scans will read the new file unless you add it to the folder's Skip list.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The copy was made and checked, or the export written. |
| 1 | The copy failed, or it is damaged; or the export couldn't be written. |
| 2 | Something is wrong with the command: a missing `--to`, a folder that isn't a keep, `--to` inside the keep, or (for `--export-tags`) a keep that needs upgrading or a file in a watched folder. Nothing was written. |

A scheduler can use these to tell you when a backup failed.

## Restoring

Close the keep, then copy `keep.db` and `keep.toml` from a backup folder into the keep's folder, replacing what's there. You can also copy them into a new folder ending in `.keep` and open that. Delete any `keep.db-wal` and `keep.db-shm` files beside the old `keep.db` first.
