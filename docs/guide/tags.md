# Tags

Tags are yours: a tree you shape however suits the collection (`Genre › Fantasy`, `Mood › Calm`, `Read`, `Favorites`). They attach to items, never to files, so they follow a file that moves.

- **Searching for a tag finds its sub-tags too:** `Genre` finds everything tagged `Genre › Fantasy` or `Genre › Science Fiction`. Putting a sub-tag on an item doesn't put its parent there; the search does the rest.
- **Tags on containers can count for what they hold:** tag an album `Live`, and with **Inherit tags** on, its songs match `Live` too. See [Searching](searching.md#toggles).

## The Tags panel

On the right of the window (**View → Tags** shows or hides it).

- **Type to find a tag:** the tree narrows as you type, matching names, other names (aliases), and descriptions, ignoring case and accents (`isl` finds `Ísland`).
- **The checks** show what the selected items have: ✓ all of them, ▣ some, empty none. On an item's page they're that item's (the panel says whose) until you select something in what the page lists; then **Back to …'s tags** above the tree goes back to the page's item. Click a check to put the tag on all selected items, or take it off all of them. A tag in *italics* comes from the files themselves (see [File keywords](file-keywords.md)).
- **Keyboard:** Ctrl+T (**Edit → Tag selection…**) jumps to the panel from anywhere; type, then **Enter** puts the highlighted tag on the selection and **Shift+Enter** takes it off. Arrows move through the tree; Esc goes back to the box.
- **New tags:** when what you typed matches no tag, **Create tag '…'** (or Enter) makes it, and puts it on the selected items. Type a path to make several levels at once: `Places > Norway` (or `Places › Norway`) makes `Norway` under `Places`, and `Places` too if it's new. A `/` isn't a separator, so `AC/DC` stays one tag.
- **Drag and drop:** drag tags onto an item to tag it, or onto a selected item to tag the whole selection. The items a drop would tag are outlined while you drag.
- **Right-click a tag** for **Show only items with '…'** or **Hide items with '…'** (adding it to the current search), or to add it to or remove it from the selection.
- **Expand all and collapse all:** every tag starts expanded. The two small buttons beside the box (⊞ and ⊟) open or close the whole tree; they're also at the end of the right-click menu.

A tag can be limited to some kinds of item (see *What a tag applies to*, below); the panel hides tags that can't go on anything selected, and tagging skips items of other kinds, saying so.

### Tag groups

When you tag one kind of thing at a time (trips one evening, people the next), a **tag group** keeps the tags you need in view. A group is a named set of tags, each with its sub-tags; a tag can be in several groups.

- **Putting tags in a group:** right-click a tag (or several selected in the tree) → **Groups** → tick a group, or **New group…** to make one with them. Untick to take them out.
- **Showing groups:** the **All tags** button beside the box (**Groups ▾**) lists your groups. Tick one or more, and the panel shows each group's name as a heading with its tags under it, in place of the whole tree. **All tags** shows everything again, and right-clicking a heading offers **Stop showing** it.
- **Nothing is out of reach:** while you type, matches outside the groups shown are listed after them, under **Other tags**.
- Groups belong to the keep, so everyone who opens it sees them; which groups the panel shows is yours alone. Making and changing groups can be undone like any tag change.

Every tagging is one step you can undo (**Edit → Undo**, Ctrl+Z).

## The tag manager

TOOLS → **Tag manager** (the Tags and Details panel steps aside while it's open): the whole tree, with how many items have each tag (**Items**) and each tag or its sub-tags (**With sub-tags**).

- **New tag…** (at the top level) and **Sub-tag…** (under the selected tag); **Rename** (F2, or double-click).
- **Move…**, or drag a tag onto another (or onto empty space for the top level). The status bar says how many items it affects.
- **Merge…** folds one tag into another: its items get the other tag, its sub-tags move under it, and its name becomes an alias of the other. It says what will happen before it does it.
- **Delete…** asks, for a tag with sub-tags, whether to delete them too or move them up a level, and says how many items will lose a tag. Items themselves are never deleted.
- The details on the right: the tag's **icon** (below), its **color**, a **description** (shown in tooltips; finding a tag matches it too), a **picture** (**Choose picture…**: shown above the description in its tooltips, up to 240 px; Tagalot keeps a copy in the keep, so the file can move or go), **other names** (aliases: they find the tag in the panel and the search box, and match [file keywords](file-keywords.md)), and **what it applies to**.
- **Undo** and **Redo** buttons on its toolbar, the same history as Edit → Undo.
- **Export tags…** and **Import tags…** save the whole tree to a file and load one back (below).

### Tag icons

A tag can have an **icon**: an emoji or one character, like ⭐ for `Favorites` or ✓ for `Read`, or a small picture where no emoji will do (a logo, a rating badge). Items show their tags' icons as small **badges**, so you can see at a glance what's a favorite.

- **Setting one:** select the tag in the tag manager, type or paste the emoji into **Icon** (on Windows, Win+. opens the emoji picker), and press Enter. Or click **Picture…** and choose a picture file (PNG, JPEG, GIF, WebP, BMP, or SVG): Tagalot keeps a small copy in the keep, so the file can move or go. An emoji and a picture replace each other; **Clear** removes either. Undo works as for any change.
- **Animated GIFs** play in the tag trees (the Tags panel and the tag manager); badges on items show their first frame, so long lists stay still.
- **Sub-tags** without an icon of their own show their parent's (or the nearest one up): give `Places` a 🌍 and `Places › Iceland` shows it too. The details say where an inherited icon comes from.
- **Where badges show:**
  - in the grid, along the bottom of each thumbnail, as many as fit (bigger thumbnails show more), then "+2" for the rest;
  - in the list's **Badges** column, as many as fit its width;
  - beside the title on the item's page, all of them.

  Point at them to see which tags they're for.
- **Only an item's own tags give badges.** An album's tags don't put badges on every song, even with **Inherit tags** on.
- Two tags with the same icon (like two sub-tags of `Read`) show it once.

### What a tag applies to

**Applies to → Change…** limits a tag to some kinds of item: `Genre` for books and comics, not authors. A sub-tag can only narrow what its parent allows. If items of other kinds already have it, Tagalot says how many and asks before taking it off them (one undoable step). Searching isn't affected: any tag can still be searched for anywhere.

### Importing and exporting tags

**Export tags…** saves the whole tree as a YAML file, a plain text file you can edit, with tags' pictures in a folder beside it (`My tags pictures/Places/Iceland.jpg`, named under `picture:`; icons that are pictures too, `Iceland icon.png` under `icon:`): **Tags only…**, or **Tags and the items that have them…** (below). **Import tags…** loads one, to start a keep with a ready-made set of tags or to reshape them in a text editor.

The file is nested like the tree. A tag can be written three ways:

```yaml
Genre:                        # the full form: properties, and sub-tags under "tags:"
  icon: 📚
  applies_to: [Book, Comic]
  tags:
    Fantasy:
    Science Fiction:
      aliases: [SF, Sci-Fi]
      color: "#4a7bd0"
      description: Rockets and robots
      groups: [Reading]
Mood: [Calm, Tense]           # a short list of sub-tags
Read:                         # just the name
```

- A short list holds names only. A sub-tag with its own sub-tags or properties goes in the full form, under `tags:`.
- `applies_to` names kinds of item as the theme does (`Book`, `Books`, either works).
- `groups` names the tag groups the tag is in. Importing puts it in them (making a group the keep hasn't) and never takes it out of others.
- Export always writes the full form.

**Importing** shows what will change before it changes anything: the new tags, the tags it updates and how, and any problems (with their line in the file).

- **Tags are matched by where they are in the tree**, ignoring case and accents, and a name also matches a tag's other names: `Sci-Fi` under `Genre` is `Genre › Science Fiction`.
- **A name spelled differently is fixed:** if the file writes `fantasy` and the tag is `Fantasy` (or `Cafe` for `Café`), the tag takes the file's spelling, so you can tidy names in a text editor. A match through another name never renames: `Sci-Fi` leaves `Science Fiction` as it is.
- **What the file says is added to what's there:** a color, description, or `applies_to` in the file replaces the tag's, other names are added, and anything the file leaves out stays as it is. To clear a color or remove another name, use the tag manager.
- **Problems are left out, and the rest imports:** a color that isn't `#rrggbb`, a kind of item this keep doesn't have, a sub-tag that applies to more than its parent allows. A file that isn't YAML imports nothing.
- **Delete tags not in this file that no item uses** (off unless you tick it) also deletes the tags the file doesn't mention, but only those no item has and with no sub-tag that stays. The preview lists them, and the ones kept because items use them. If you renamed a tag in the file and items use it, the old one stays: merge it into the new one with **Merge…**.

An import is one step: **Undo** puts every tag back as it was, and takes the tags it put on items off again.

#### Which items have each tag

**Export tags… → Tags and the items that have them…** adds, after the tree, every item with tags of your own: to back your tagging up, or to carry it to another keep with the same theme. Tags that come from files' own keywords aren't written: they come back from the files.

```yaml
---
items:
- type: Book
  title: Small Gods
  key: book:humble bundle|small gods
  files: [Humble Bundle/Small Gods.epub]
  tags: [Genre > Fantasy, Read]
```

- **Importing finds each item** by its `key` (Tagalot's own name for it), else by one of its `files` (relative to its watched folder, so another drive or share works), else by its kind and `title` when exactly one item has them. An item found no way is listed in the preview as not found; it's never guessed.
- **Its tags are added;** nothing is taken off, unless you tick **Also take tags off its items that the file doesn't list**: then each item the file lists ends up with exactly its tags from the file (tags from its files' keywords stay).
- The preview says how many items were found, how many tags it will add (and take off), and lists the items not found. A tag the file gives an item must be in the keep or in the file's tree.
- A file can hold only the items (starting with `---` or with `items:`), and you can edit it: add an item, or change its tags.
