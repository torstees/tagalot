# Writing to files

Tagalot never changes your files, with one narrow, opt-in exception: **Write to file…** can write an item's tags and the details you edited into the **front matter of its Markdown files**, and of **its note**. So tags you set in Tagalot also live in the files (for Obsidian and similar tools): a Markdown book, a Markdown file in any keep, or the note of an author, a person, a series, a collection, or a folder.

## Turning it on, folder by folder

It's off everywhere until you allow it for a folder: **Keep → Configure keep… → Folders**, select the folder, and tick **Write back** ("Let Write to file… change files in this folder"; it asks first). No other folder is ever written to.

Notes in the [notes folder](notes.md#the-notes-folder) have a switch of their own, so you can allow them without allowing the rest of their folder: **Keep → Configure keep… → Notes → Let Write to file… change notes' front matter here** (it asks first). It's separate from **Let Save as note… add new notes**, which only ever adds new files.

## Writing

Select items and choose **Write to file…** (right-click, or More ▾ on an item's page; it's there for every item). Nothing is written yet: Tagalot reads the files and shows a preview of each one's front matter, before and after, with a box to leave it out. A note is marked "(its note)" in the list. It also says why a file won't be written: its folder doesn't allow it, it changed since Tagalot last read it, its front matter doesn't parse, or it's a note two items share (a movie's folder note is its folder's too, so the movie's tags would go to the folder as well), or it's inside an archive, which Tagalot never changes.

**Write N files** then, for each file still ticked:

1. checks it's still as Tagalot read it (else it's skipped and reported);
2. copies it to the keep's `backups/` folder;
3. writes the new version beside it and puts it in place.

Afterwards Tagalot scans those files, so it reads what it wrote.

## What's written

- **Only the front matter changes** (the block between its `---` or `+++` lines); the rest of the file is kept byte for byte. YAML keeps its comments, quotes, key order, and list style. A file without front matter gains a YAML block at the top.
- **Tags** go under the file's `tags` key (or `keywords`): the file's own words stay, less any whose tag you took off the item; your other tags are added as full paths (`Genre/Fantasy`, as Obsidian writes nested tags).
- **Fields you edited** in Tagalot, under the keys the file already uses where it has them. In a Markdown book (title, number in series, year, publisher, language, ISBN, description, link, assets folder, rating): `number` rather than `series_index`, `synopsis` rather than `description`, say. Elsewhere, and in notes, each field goes under the key the file uses for it (its name or label, ignoring case, spaces, `_`, and `-`, as notes are read: `sort-name` for **Sort name**), else under the field's name.
- **Your own fields** you set in Tagalot, the same way: under the key the file uses for the field (its name or label), else under its name (`pages: 412`).

It isn't an Edit → Undo step: the backups are the way back. Each write makes a new `backups/<date and time>/` folder in the keep.
