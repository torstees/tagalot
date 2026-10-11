# Notes

A **note** is a Markdown file that describes an item: what no file says, like a friendlier name for a folder, an artist's background, or where a collection came from. Tagalot reads notes and shows them on the item's page; it never changes them.

## Where a note goes

Put the note **inside the item's folder**, named after the folder:

```
Aurora Studio/
  Aurora Studio.md      ← the note
  sky-01.png
  …
```

`index.md` and `README.md` work too (and `.markdown`), so a folder you already describe for another program needs nothing new. When a folder has more than one, the one named after the folder wins, then `index`, then `README`.

This works for items that *are* folders: an artist in the [2D assets](../themes/assets2d.md) theme, an album in [Music](../themes/music.md), a collection in [Movies](../themes/movies.md). Notes are read at the next scan, and read again whenever you change them.

## What a note gives

A note's front matter (the block between `---` lines at the top) gives details; the rest is its body.

```markdown
---
title: Aurora Studio (Reykjavík)
tags: [Landscapes, Skies]
website: https://aurora.example
---
# Aurora Studio (Reykjavík)

Painted skies and dusk light, since 2009.
```

- **`title`:** the item's name, in place of the folder's.
- **`tags`** (or `keywords`): [file keywords](file-keywords.md). They become tags when they match tags you have.
- **A key matching one of the item's fields**, by its name or label (`year`, `Genre`): that field, where no file gives one. What the files themselves say still wins, and a note's value replaces one looked up online.
- **Any other key** becomes an **Extra field**, marked "• from the note".
- **The body** shows under the item's title, and is searchable.

Your own edits always win: a name or field you changed by hand is never replaced by the note.

## On the item's page

The note's body shows in the page's header, beside the picture. A long note is cut short at the picture's height: **Show more** shows the rest, and **Show less** cuts it again. Pictures and links in the note are found from the note's folder (`![](studio.png)`, `[prices](Prices/list.txt)`); web links open in your browser. Obsidian's `![[picture.png]]` and `[[links]]` work too.

**Open note** (under the note, and in **More ▾**) opens the note file in your Markdown editor, which is where you change it.

### Extra fields from a note

Extra fields that came from the note are marked **• from the note**, and scans keep them up to date as the note changes.

- **Edit one** to make it yours: it's marked "• edited", and scans leave it alone.
- **Remove one** (its ×) and later scans leave it off for this item, even though the note still has it.

Both are one step you can undo.
