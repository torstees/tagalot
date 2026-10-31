# Backups

Your tags, notes, and everything else you did by hand live in the keep's `keep.db`. Your files are never changed, but the keep is worth backing up, ideally on a schedule.

The easy way is **Keep configuration → Backups** ([below](#in-keep-configuration)): choose a folder and a time, and Tagalot sets up the schedule on this computer. Underneath, it runs commands you can also run, or schedule, yourself ([Running on a schedule](#running-on-a-schedule)):

- **`tagalot --backup`:** a complete copy of the keep, to restore from, with the tag export in it if you like (`--tags`).
- **`tagalot --export-tags`:** your tags, in a file you can read, edit, and load into any keep.

## In Keep configuration

**Keep → Configure keep… → Backups** backs the open keep up on a schedule, on this computer:

- **Back up to:** a folder, ideally on another disk or a network share. It can't be inside the keep's folder, or in a folder the keep watches: Tagalot never writes there.
- **How often:** every day, or one day of the week, at a time. A backup missed while the computer was off is made when it's next on (on macOS, only one missed during sleep, not while shut down).
- **Keep the last:** how many backups to keep; older ones are deleted.
- **Also save:** each backup also holds the tag export, with the items that have them (`<keep> tags.yaml`, as **Export tags…** saves it).

**Schedule backups** says what it will add, then adds it the system's own way:

| | What it adds |
|---|---|
| **Windows** | A task in Task Scheduler, under **Tagalot** |
| **macOS** | A launchd agent in `~/Library/LaunchAgents` |
| **Linux** | A systemd user timer (`~/.config/systemd/user`), or a line in your crontab where there's no systemd |

Then the tab shows the schedule and the **Last backup**: what the last run printed, from `<keep> last run.txt` in the backup folder. **Update schedule** changes it, and **Remove schedule** takes it away; your choices are remembered for this computer. **Back up now** makes a backup at once, and **Copy command** copies the command the schedule runs, to set it up by hand.

The schedule belongs to this computer: a keep opened on another computer isn't backed up there until you schedule it there too.

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

- **`--tags`:** each copy also holds the tag export, with the items that have them (`Movies tags.yaml`, and its pictures), read from the copy itself. If only the export fails, the copy still counts, and the command fails with the reason in the log.
- **Not in a watched folder:** `--to` can't be inside the keep's folder, or in a folder the keep watches.

Thumbnails and the text read from documents aren't copied: Tagalot makes them again as needed.

On Windows, run `tagalot-cli.exe` rather than `tagalot.exe` ([below](#running-on-a-schedule)), so you see what it prints. From source, run `uv run tagalot --backup …`.

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

## Running on a schedule

Which command to run depends on how you installed Tagalot:

| | The command |
|---|---|
| **Windows** | `tagalot-cli.exe`, beside `tagalot.exe`: installed, `%LOCALAPPDATA%\Programs\Tagalot\tagalot-cli.exe` (or `C:\Program Files\Tagalot\` when installed for everyone). |
| **macOS** | `/Applications/Tagalot.app/Contents/MacOS/tagalot` |
| **Linux** | The `.AppImage` itself, or `tagalot` in the unpacked folder. |
| **From source** | `uv run --project /path/to/tagalot tagalot` |

On Windows, use **`tagalot-cli.exe`**, not `tagalot.exe`. `tagalot.exe` opens a window and has no console, so a scheduled run would print nothing and its exit code would be hard to see. `tagalot-cli.exe` runs the same commands in a console and returns their exit codes. It only runs commands: started on its own, it lists them.

### Windows Task Scheduler

In PowerShell, this backs the keep up every night at 3:00 and keeps two weeks of copies, then exports the tags beside them. Each command is one line (the `-Argument` lines are long): paste them as they are.

```powershell
$cli = "$env:LOCALAPPDATA\Programs\Tagalot\tagalot-cli.exe"
$backup = New-ScheduledTaskAction -Execute $cli -Argument '--backup "D:\Keeps\Movies.keep" --to "E:\Backups\Tagalot" --keep-last 14 --output "E:\Backups\Tagalot\last backup.txt"'
$tags = New-ScheduledTaskAction -Execute $cli -Argument '--export-tags "D:\Keeps\Movies.keep" --to "E:\Backups\Tagalot\Movies tags.yaml" --items --output "E:\Backups\Tagalot\last tag export.txt"'
$trigger = New-ScheduledTaskTrigger -Daily -At 3am
Register-ScheduledTask -TaskName "Tagalot backup" -Action $backup, $tags -Trigger $trigger
```

Before you rely on it, check what the task will run. Each action should show the program, then your arguments, and an empty working directory:

```powershell
(Get-ScheduledTask "Tagalot backup").Actions | Format-List Execute, Arguments, WorkingDirectory
```

Or set it up in the **Task Scheduler** app:

1. Choose **Create Basic Task…**, name it, and choose **Daily** and a time.
2. Choose **Start a program**: the program is `tagalot-cli.exe` (Browse to it), and the arguments are `--backup "D:\Keeps\Movies.keep" --to "E:\Backups\Tagalot" --keep-last 14 --output "E:\Backups\Tagalot\last backup.txt"`. Leave **Start in** empty.

Use full paths everywhere: a scheduled task doesn't start in your folder, so `scratch\Demo.keep` wouldn't be found.

The console window flashes up while the task runs, if you're signed in. In the task's **Properties**, **Run whether user is logged on or not** runs it out of sight.

### Seeing what went wrong

A scheduled run has no one watching, so look in three places:

- **What it printed:** `--output FILE` writes everything the command prints to that file, errors included, replacing it each run (the examples above use `last backup.txt`). Without it, a run's messages are lost.
- **`backup.log`** in the backup folder: a line for each backup that got as far as copying, `ok` or `FAILED` with why. A mistake in the command (a keep that isn't there) stops it before then: see `--output`'s file.
- **The task's Last Run Result** in Task Scheduler (or `Get-ScheduledTaskInfo "Tagalot backup"`, as `LastTaskResult`):

  | Result | Meaning |
  |---|---|
  | `(0x0)` | It worked. |
  | `(0x1)` | The copy failed or is damaged, or the export couldn't be written. |
  | `(0x2)` | A mistake in the command: see the `--output` file. |
  | `(0x41301)` | It is running now. |
  | Anything else (`0x800710E0`, `0x80070002`…) | Windows couldn't start `tagalot-cli.exe` at all: check the action's program path, and that **Start in** is empty or a folder. Nothing was printed or logged. |

### cron (Linux and macOS)

`crontab -e`, then a line like this one, for every night at 3:00:

```
0 3 * * * "$HOME/Applications/Tagalot.AppImage" --backup "$HOME/Keeps/Movies.keep" --to /mnt/backup/Tagalot --keep-last 14
```

cron mails or logs what a command prints, and `backup.log` has a line for each run. On macOS, use the path in the table above. cron may need **Full Disk Access** (System Settings → Privacy & Security) to reach other disks.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The copy was made and checked, or the export written. |
| 1 | The copy failed, or it is damaged; or the export (with `--tags`, the one in the copy) couldn't be written. |
| 2 | Something is wrong with the command: a missing `--to`, a folder that isn't a keep, `--to` inside the keep or a folder it watches, or (for `--export-tags`) a keep that needs upgrading or a file in a watched folder. Nothing was written. |

A scheduler can use these to tell you when a backup failed.

## Restoring

Close the keep, then copy `keep.db` and `keep.toml` from a backup folder into the keep's folder, replacing what's there. You can also copy them into a new folder ending in `.keep` and open that. Delete any `keep.db-wal` and `keep.db-shm` files beside the old `keep.db` first.
