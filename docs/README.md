# Tagalot

Tagalot is a tag-based file browser for your collections: music, movies, books, papers, 2D art, or plain files. You point a **keep** at your folders (local or on a network share); Tagalot reads them into **items** you can tag in a hierarchy, search, and browse. It never changes your files.

![Tagalot showing a music keep's albums and songs](images/screens/music-browse.png)

## The ideas

- **A keep** is a folder of Tagalot's own (`Music.keep`) that remembers everything about one collection: the folders it watches, the items it found, your tags. Keeps are independent: one for music, one for papers.
- **Folders** are the places a keep watches. They stay exactly as they are; Tagalot only reads them. A folder on a share that's offline is shown as offline, never forgotten.
- **Items** are what your files are: a song, an album, a movie, a paper. One item can have several files (an album's songs, a paper's preprint and published version), and items can hold others (an artist's albums).
- **Tags** form a tree: `Genre › Fantasy`, `Mood › Calm`. Searching for a tag finds its sub-tags too, and tags on a container can count for what it holds.
- **A theme** decides what the items are. Tagalot comes with six: [Files](themes/files.md), [2D assets](themes/assets2d.md), [Music](themes/music.md), [Movies](themes/movies.md), [Books](themes/books.md), and [Research](themes/research.md). You can [write your own](THEMES.md).

## What it does

- **Scans** your folders quickly, network shares included, and notices what's new, changed, moved, or missing.
- **Tags** one item or thousands at once: tick a tag, press Enter, or drag a tag onto a selection. Every change can be undone.
- **Searches** by tags (with or without their sub-tags), text, fields, and what items contain; saves searches you use often.
- **Shows** items as a grid of thumbnails, a list, or a tree, with a page for each item.
- **Tidies up:** finds untagged items, files nothing uses, missing files, and duplicates.
- **Searches inside documents** (papers and books) when you want it to.
- **Looks details up online** (papers by DOI, books by ISBN) only if you allow it, keep by keep.

## Where to start

- [Install Tagalot](getting-started/install.md), then [make your first keep](getting-started/first-keep.md).
- Or try a [demo keep](getting-started/demo-keeps.md) first: ready-made keeps with sample files and tags.
- [The window](guide/window.md) is a tour of everything on screen.
