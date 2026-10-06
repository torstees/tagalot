# Writing to files

Tagalot never changes your files, with one narrow, opt-in exception: **Write to file…** can write an item's tags and the details you edited into the **front matter of its Markdown files**. It exists for the [Books](../themes/books.md) theme's Markdown stories, so tags you set in Tagalot also live in the files (for Obsidian and similar tools).

## Turning it on, folder by folder

It's off everywhere until you allow it for a folder: **Keep → Configure keep… → Folders**, select the folder, and tick **Write back** ("Let Write to file… change files in this folder"; it asks first). No other folder is ever written to.

## Writing

Select items and choose **Write to file…** (right-click, or More ▾ on an item's page). Nothing is written yet: Tagalot reads the files and shows a preview of each one's front matter, before and after, with a box to leave it out. It also says why a file won't be written: its folder doesn't allow it, it changed since Tagalot last read it, or its front matter doesn't parse.

**Write N files** then, for each file still ticked:

1. checks it's still as Tagalot read it (else it's skipped and reported);
2. copies it to the keep's `backups/` folder;
3. writes the new version beside it and puts it in place.

Afterwards Tagalot scans those files, so it reads what it wrote.

## What's written

- **Only the front matter changes** (the block between its `---` or `+++` lines); the rest of the file is kept byte for byte. YAML keeps its comments, quotes, key order, and list style. A file without front matter gains a YAML block at the top.
- **Tags** go under the file's `tags` key (or `keywords`): the file's own words stay, less any whose tag you took off the item; your other tags are added as full paths (`Genre/Fantasy`, as Obsidian writes nested tags).
- **Fields you edited** in Tagalot (title, number in series, year, publisher, language, ISBN, description, link), under the keys the file already uses where it has them (`number` rather than `series_index`, say).

It isn't an Edit → Undo step: the backups are the way back. Each write makes a new `backups/<date and time>/` folder in the keep.
