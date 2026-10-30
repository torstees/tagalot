# Duplicates

TOOLS → **Dedupe** finds files that are the same, and items that look alike. It never deletes files.

## Identical files

Files with the same fingerprint, grouped, the most space wasted first ("3 groups of identical files; the extra copies take 12.4 MB"). Each group expands to its copies: where each is, its size, and its items.

A fingerprint is quick to take but samples the file. **Check** reads the selected groups' files whole to be sure (**Check all** does every group); each copy then says *same*, *differs*, or *couldn't read* (offline, say).

Files inside archives the keep reads are compared too, so a font in a zip and a loose copy of it are one group. They show as the archive, then the path inside it (`pack.zip › fonts/Regular.ttf`); **Open file** opens a copy, **Show the archive in file manager** shows the archive, and **Hide inside its archive** stops reading that copy (its item hides, keeping its tags).

## Similar items

The theme's near-duplicates, most alike first: songs with the same artist and title and about the same length, pictures that look the same, books by one writer with nearly the same title, papers sharing a DOI or arXiv ID.

## Linked folders

Folders with the same name whose files match by name, size, and fingerprint, the most matches first: a folder your share shows as an ordinary folder when it's really a link to another (a bundle's folder linked into an author's, say). The table gives how many files match, differ, and are on one side only; select a pair to see them listed beside it.

- **Link folders** makes them one folder in two places: each file that matches becomes one item, listed in both folders, with the files' items merged into the first folder's (as Merge does). A file that turns up in both later is linked by the next scan when the two match; one that differs is listed in [Triage → Links](triage.md#links) instead.
- **Files that differ block linking**, with a warning: two places of one file can't differ, so they may be copies, one edited. A file changing while it was read, or a network glitch, can also make one look different: **Check again** reads them whole, and if they're the same after all, linking is allowed.
- **Not the same folder** hides the pair, as Not a duplicate does.

Linking is a step you can undo. **Unlink this place**, on an item's page, separates one file again.

## Comparing and merging

Select a group or pair and the **compare pane** shows the items side by side: their pictures, fields, your own fields, tags, contents, and files (formats, sizes, places), with differences highlighted. Then:

- **Merge…** makes them one item: pick which to keep, and, where your own edited values differ, which wins. The kept item gets the others' tags, files, containers, and relationships; the others are removed. Their files become the kept item's versions where the kind allows it (songs, movies, books, papers).
- **Keep both as versions** (or *Keep all*) merges at once into the left item, for kinds whose items have several files.
- **Link as one** (identical files) is for copies that are really one file in several places, such as links your share shows as ordinary files. They become one item, listed in each place, with the files' items merged into the first copy's (as Merge does), and later scans keep them together. They then leave this page, since they're one file. It isn't offered once **Check** found the copies different. **Unlink this place** on the item's page separates one again.
- **Not a duplicate** hides the group or pair. A group shows again if its files change. **Show dismissed** lists what you've hidden, to show it again.

Merging, linking, and dismissing are steps you can undo. A merged item stays merged: scanning its old file again doesn't bring it back.
