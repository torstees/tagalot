# Duplicates

TOOLS → **Dedupe** finds files that are the same, and items that look alike. It never deletes files.

## Identical files

Files with the same fingerprint, grouped, the most space wasted first ("3 groups of identical files; the extra copies take 12.4 MB"). Each group expands to its copies: where each is, its size, and its items.

A fingerprint is quick to take but samples the file. **Check** reads the selected groups' files whole to be sure (**Check all** does every group); each copy then says *same*, *differs*, or *couldn't read* (offline, say).

## Similar items

The theme's near-duplicates, most alike first: songs with the same artist and title and about the same length, pictures that look the same, books by one writer with nearly the same title, papers sharing a DOI or arXiv ID.

## Comparing and merging

Select a group or pair and the **compare pane** shows the items side by side: their pictures, fields, your own fields, tags, contents, and files (formats, sizes, places), with differences highlighted. Then:

- **Merge…** makes them one item: pick which to keep, and, where your own edited values differ, which wins. The kept item gets the others' tags, files, containers, and relationships; the others are removed. Their files become the kept item's versions where the kind allows it (songs, movies, books, papers).
- **Keep both as versions** (or *Keep all*) merges at once into the left item, for kinds whose items have several files.
- **Not a duplicate** hides the group or pair. A group shows again if its files change. **Show dismissed** lists what you've hidden, to show it again.

Merging and dismissing are steps you can undo. A merged item stays merged: scanning its old file again doesn't bring it back.
