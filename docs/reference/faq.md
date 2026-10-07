# FAQ and troubleshooting

## Can my files be on a network share?

Yes: watch `\\nas\music`, a mapped drive, or a mounted volume. Scans read in the background, and a share that's offline is marked offline, never forgotten. Only the **keep itself** should be on a local disk: its databases (SQLite) aren't reliable over network file systems, so Tagalot warns when you open a keep from one.

## The same keep on two computers

Copy or sync the keep's folder (not while it's open on both). If the files are reached by different paths on each computer, set **On this computer** for each folder in Keep configuration; it's saved per computer, not in the keep. A keep is meant for one person at a time.

## RAR archives show only an icon

Listing a RAR needs nothing, but reading an image from it needs a RAR tool on your `PATH`: `unrar`, 7-Zip (`7z`), or `bsdtar`. Install one and the next thumbnails will work. `tagalot --check` says whether one was found.

## Some files say "Can't read"

The [Activity panel](../guide/activity.md) lists them with the reason. Usually: permissions (the account running Tagalot can't read that folder or file), a damaged file, or a file in use. Tagalot carries on with the rest, and tries again at the next scan once a file changes.

## A file I deleted is still listed

It's marked **missing**, not forgotten, in case it comes back (a folder renamed, a drive remounted). [Triage](../guide/triage.md) → **Missing files** lists those items; delete them there once you're sure.

## A folder shows as offline

Tagalot couldn't reach it at the last scan (a share that's down, a drive not plugged in, a path that changed). Its items and tags are all still there. Fix the path in Keep configuration (or **On this computer**), then scan.

## Tagalot changed a tag I didn't touch

Tags in *italics* come from the files themselves ([file keywords](../guide/file-keywords.md)): they follow the files. Taking one off an item by hand sticks; to stop a keyword giving a tag anywhere, remove its alias from the tag in the Tag manager, or ignore it on the File keywords page.

## A value I edited keeps changing back

It shouldn't: edited values are marked "• edited" and scans leave them alone. If it lacks the mark, the edit didn't save (an invalid value stays in the box with the reason). **Re-read from file, replacing my edits…** is the one command that puts the file's value back.

## Thumbnails are wrong or stale

Thumbnails are remade when a scan sees a file changed. To remake them all: **Keep → Clear thumbnail cache…** (they're made again as you browse).

## Can I undo a scan?

No; but scans never delete your tags or edits, never touch files, and never forget an offline folder's items. Removing a folder **with its items** (Keep configuration → Remove…) is the one thing that deletes items in bulk, and it asks twice.

## Where are the logs?

Problems with files are in the [Activity panel](../guide/activity.md). Running from a terminal (`uv run tagalot`) prints Tagalot's log there.

## How do I report a problem?

Open an issue at [github.com/torstees/tagalot/issues](https://github.com/torstees/tagalot/issues), with what you did and what `tagalot --check` says (**Help → About Tagalot → Copy check report** copies it).
