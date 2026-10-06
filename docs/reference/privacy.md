# Privacy and your files

## Your files

**Tagalot reads your files; it doesn't change them.** It never writes, moves, renames, or deletes anything in the folders a keep watches. Tags, edits, and everything else you do live in the keep's own folder. Deleting an item, removing a folder from a keep, or clearing thumbnails never touches a file.

There are two exceptions, both things you ask for, one at a time:

- **[Write to file…](../guide/write-to-file.md)** writes tags and edited details into Markdown files' front matter: only in folders you've allowed, only when you choose the command, after showing what will change, and with a backup of each file first.
- **Saving an export** (such as the research theme's **Export BibTeX…**) writes the new file where you choose. Inside a watched folder it warns first, never replaces an existing file, and adds the new file to that folder's Skip list.

What Tagalot writes elsewhere: the keep's folder (its databases, thumbnails, backups), your [settings](keep-files.md#settingstoml), and temporary files (such as a playlist for **Play album**) in your system's temporary folder, deleted when the keep closes.

## Going online

**Tagalot doesn't go online, except to look up items' details, and only after you allow it for the keep.** The first time a lookup would happen, it asks, naming each service and what it would be sent:

| Theme | Service | Sends |
|---|---|---|
| Research | Crossref (`api.crossref.org`) | DOIs |
| Research | arXiv (`export.arxiv.org`) | arXiv IDs |
| Books | Open Library (`openlibrary.org`) | ISBNs |

Only those identifiers, in the addresses requested, are sent: no file names, titles, tags, or anything else about your files. There's no account, no key, no telemetry, and no update check. Answers are kept in the keep. See [Online details](../guide/online-details.md).

Other things that open the web do so in your browser, when you click them: a book's or paper's link, **Open DOI page**.

## Themes

Themes are Python code that runs inside Tagalot. The built-in ones follow the rules above; a theme you install yourself could do anything a program can, so install themes only from people you trust.
