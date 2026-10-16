# Notes

A **note** is a Markdown file that describes an item: what no file says, like a friendlier name for a folder, an artist's background, or where a collection came from. Tagalot reads notes and shows them on the item's page; it never changes them. An item without a note file can have a note [typed in Tagalot](#typing-a-note-in-tagalot), and **Save as note…** can make it a file.

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

## The notes folder

Items with no folder of their own (an actor, an author, a music artist) take their notes from the keep's **notes folder**. Choose it in [Keep configuration → Notes](keep-configuration.md#notes): a folder inside a watched folder, or one anywhere else, which Tagalot then watches for notes only. Tagalot never treats a folder as the notes folder because of its name.

```
Notes/
  Actors/
    Al Pacino.md        ← the note for the actor "Al Pacino"
  Keanu.md              ← tagalot-type: actor, aliases: [Keanu Reeves]
```

- **Which item:** the note's `title`, else its file name, or one of its `aliases`, matches the item's title. Case and accents don't matter (`Leon.md` finds "Léon").
- **Which kind of item:** the note's `tagalot-type` key (`actor`, `Actors`, a type's name or label, singular or plural; a type keeps its old name when the theme renames it, so the movies theme's `Actors/` still finds its People), else the name of the folder it's in (`Actors/`), else any kind. The key wins, so a note in the wrong folder can say what it's for.
- **Nothing else is read there:** pictures and other files in the notes folder aren't items. Its Markdown files are read as notes, and its pictures only for galleries: a folder or archive named like a note, beside it (`Actors/Al Pacino/`, `Al Pacino.zip`), is that item's gallery of photos or covers ([Galleries](item-pages.md#galleries)).
- **One note per item.** A note in the item's own folder wins over one in the notes folder.

Notes that match no item, or several, and notes for an item that already has a note, are listed in [Triage → Markdown](triage.md#markdown), as is any Markdown file whose front matter gives a key twice or doesn't parse. A new item finds a note that was already there at the next scan, so you can write notes before their items appear.

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
- **`cover`** (or `image`): the item's picture, a file relative to the note (`cover: pictures/pratchett.jpg`, or from the top of the watched folder with a leading `/`). It's shown for the item everywhere, before the picture the theme would choose. If the file isn't there, [Triage → Pictures](triage.md#pictures) lists it. In the notes folder, a picture named like the note beside it (`Terry Pratchett.jpg` beside `Terry Pratchett.md`) does the same without a key.
- **A key matching one of the item's fields**, by its name or label (`year`, `Genre`): that field, where no file gives one. What the files themselves say still wins, and a note's value replaces one looked up online.
- **Any other key** becomes an **Extra field**, marked "• from the note", unless it is [ignored](#keys-to-ignore).
- **The body** shows under the item's title, and is searchable.

Your own edits always win: a name or field you changed by hand is never replaced by the note.

## On the item's page

The note's body shows in the page's header, beside the picture. A long note is cut short at the picture's height: **Show more** shows the rest, and **Show less** cuts it again. Pictures and links in the note are found from the note's folder (`![](studio.png)`, `[prices](Prices/list.txt)`); web links open in your browser. Obsidian's `![[picture.png]]` and `[[links]]` work too.

A picture or link that isn't beside the note is looked for in an **assets folder**: the note's own `assets` key (`assets: Attachments`, relative to the note, or from the top of its watched folder with a leading `/`; it shows as an Extra field you can change), else the watched folder's or the keep's (Keep configuration, [Folders](keep-configuration.md#folders) and [Keep](keep-configuration.md#keep)). The name is kept as written: `![](art/glow.png)` finds `Attachments/art/glow.png`.

**Open note** (under the note, and in **More ▾**) opens the note file in your Markdown editor, which is where you change it.

### Extra fields from a note

Extra fields that came from the note are marked **• from the note**, and scans keep them up to date as the note changes.

- **Edit one** to make it yours: it's marked "• edited", and scans leave it alone.
- **Remove one** (its ×) and later scans leave it off for this item, even though the note still has it.
- **Ignore the key in every note:** the arrow beside its × → **Ignore this key in every note…**. After you agree, the fields notes gave under that key are removed from every item in the keep, and scans leave the key out from then on. Fields you added or edited yourself stay. The keep's list in [Keep configuration → Notes](keep-configuration.md#notes) gains the key.

Each is one step you can undo.

A Markdown book's own front matter gives Extra fields the same way, marked **• from the file** ([Books](../themes/books.md)). Edit, remove, and ignore them as above. When a book's file and its note give the same key, the file's value wins.

## Typing a note in Tagalot

An item without a note file shows **Add a note** under its name. Click it, type (Markdown works: `**bold**`, lists, links), and press **Ctrl+Enter** or click away to save. **Esc** cancels; **Enter** starts a new line. The note is shown like a note file's, and **Search all** finds its words.

To change it, click ✎ beside it or double-click it. Empty the box to remove it. Each change is one step you can undo (**Edit → Undo**).

A typed note is kept in the keep, not in a file. If a note file for the item turns up later (in its folder, or in the notes folder), the file's note takes over, since a note is edited in one place. If the two say different things, the [activity panel](activity.md) says so and quotes the start of the typed note, so you can copy anything you need into the file.

### Save as note…

**More ▾ → Save as note…** writes the item's typed note as a new file in the [notes folder](#the-notes-folder), so you can keep it with your files and edit it in your own editor. It's there once Tagalot may add notes to the notes folder (**Let Save as note… add new notes to this folder** in [Keep configuration → Notes](keep-configuration.md#notes); it asks first).

Tagalot shows where the file will go and asks before writing:

```
Notes/
  Actors/
    Robert De Niro.md
```

```
---
title: Robert De Niro
tags:
- Genre/Crime
---

Born in Greenwich Village.
```

- **Where:** in a folder for each kind of item (`Actors/`), or all in the notes folder with a `tagalot-type` key, as Keep configuration says.
- **What:** the item's title and its tags (as full paths), then the note.
- **Never over a file, never a second note:** if a file of that name is there, or one where the other layout would put it (`Actors/Robert De Niro.md` or `Robert De Niro.md`), nothing is written and Tagalot says so. Characters some systems don't allow in file names (`:` `?` `*` `/` `\` `"` `<` `>` `|`) become `_`.
- **Afterwards** Tagalot scans the folder, and the item's note is the file's: **Open note** edits it from then on.

Nothing else is ever written in the notes folder, or in the watched folder it's in.

## Keys to ignore

Some keys are housekeeping for your Markdown editor, not details of the item. Keys that are ignored never become Extra fields. Three lists apply together:

- **Always:** keys editors add for themselves: `cssclass`, `cssclasses`, `permalink`, `position`, `publish`.
- **In this keep:** saved with the keep (in `keep.toml`), so they go where the keep goes.
- **In all your keeps:** saved for you on this computer (in `settings.toml`).

Edit the last two in [Keep configuration → Notes](keep-configuration.md#notes), one key per line; case doesn't matter. A change applies at the next scan (**F5**), which reads the notes (and Markdown files' front matter) again: a key you stop ignoring comes back as an Extra field. Keys starting with `tagalot-` are Tagalot's own and can't be ignored.
