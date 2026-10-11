# Tags

Tags are yours: a tree you shape however suits the collection (`Genre › Fantasy`, `Mood › Calm`, `Read`, `Favorites`). They attach to items, never to files, so they follow a file that moves.

- **Searching for a tag finds its sub-tags too:** `Genre` finds everything tagged `Genre › Fantasy` or `Genre › Science Fiction`. Putting a sub-tag on an item doesn't put its parent there; the search does the rest.
- **Tags on containers can count for what they hold:** tag an album `Live`, and with **Inherit tags** on, its songs match `Live` too. See [Searching](searching.md#toggles).

## The Tags panel

On the right of the window (**View → Tags** shows or hides it).

- **Type to find a tag:** the tree narrows as you type, matching names, other names (aliases), and descriptions, ignoring case and accents (`isl` finds `Ísland`).
- **The checks** show what the selected items have: ✓ all of them, ▣ some, empty none. Click a check to put the tag on all selected items, or take it off all of them. A tag in *italics* comes from the files themselves (see [File keywords](file-keywords.md)).
- **Keyboard:** Ctrl+T (**Edit → Tag selection…**) jumps to the panel from anywhere; type, then **Enter** puts the highlighted tag on the selection and **Shift+Enter** takes it off. Arrows move through the tree; Esc goes back to the box.
- **New tags:** when what you typed matches no tag, **Create tag '…'** (or Enter) makes it, and puts it on the selected items. Type a path to make several levels at once: `Places > Norway` (or `Places › Norway`) makes `Norway` under `Places`, and `Places` too if it's new. A `/` isn't a separator, so `AC/DC` stays one tag.
- **Drag and drop:** drag tags onto an item to tag it, or onto a selected item to tag the whole selection. The items a drop would tag are outlined while you drag.
- **Right-click a tag** for **Show only items with '…'** or **Hide items with '…'** (adding it to the current search), or to add it to or remove it from the selection.

A tag can be limited to some kinds of item (see *What a tag applies to*, below); the panel hides tags that can't go on anything selected, and tagging skips items of other kinds, saying so.

Every tagging is one step you can undo (**Edit → Undo**, Ctrl+Z).

## The tag manager

TOOLS → **Tag manager**: the whole tree, with how many items have each tag (**Items**) and each tag or its sub-tags (**With sub-tags**).

- **New tag…** (at the top level) and **Sub-tag…** (under the selected tag); **Rename** (F2, or double-click).
- **Move…**, or drag a tag onto another (or onto empty space for the top level). The status bar says how many items it affects.
- **Merge…** folds one tag into another: its items get the other tag, its sub-tags move under it, and its name becomes an alias of the other. It says what will happen before it does it.
- **Delete…** asks, for a tag with sub-tags, whether to delete them too or move them up a level, and says how many items will lose a tag. Items themselves are never deleted.
- The details on the right: the tag's **color**, a **description** (shown in tooltips; finding a tag matches it too), **other names** (aliases: they find the tag in the panel and the search box, and match [file keywords](file-keywords.md)), and **what it applies to**.
- **Undo** and **Redo** buttons on its toolbar, the same history as Edit → Undo.
- **Export tags…** and **Import tags…** save the whole tree to a file and load one back (below).

### What a tag applies to

**Applies to → Change…** limits a tag to some kinds of item: `Genre` for books and comics, not authors. A sub-tag can only narrow what its parent allows. If items of other kinds already have it, Tagalot says how many and asks before taking it off them (one undoable step). Searching isn't affected: any tag can still be searched for anywhere.

### Importing and exporting tags

**Export tags…** saves the whole tree as a YAML file, a plain text file you can edit. **Import tags…** loads one, to start a keep with a ready-made set of tags or to reshape them in a text editor. Only the tags travel, not which items have them.

The file is nested like the tree. A tag can be written three ways:

```yaml
Genre:                        # the full form: properties, and sub-tags under "tags:"
  applies_to: [Book, Comic]
  tags:
    Fantasy:
    Science Fiction:
      aliases: [SF, Sci-Fi]
      color: "#4a7bd0"
      description: Rockets and robots
Mood: [Calm, Tense]           # a short list of sub-tags
Read:                         # just the name
```

- A short list holds names only. A sub-tag with its own sub-tags or properties goes in the full form, under `tags:`.
- `applies_to` names kinds of item as the theme does (`Book`, `Books`, either works).
- Export always writes the full form.

**Importing** shows what will change before it changes anything: the new tags, the tags it updates and how, and any problems (with their line in the file).

- **Tags are matched by where they are in the tree**, ignoring case and accents, and a name also matches a tag's other names: `Sci-Fi` under `Genre` is `Genre › Science Fiction`.
- **A name spelled differently is fixed:** if the file writes `fantasy` and the tag is `Fantasy` (or `Cafe` for `Café`), the tag takes the file's spelling, so you can tidy names in a text editor. A match through another name never renames: `Sci-Fi` leaves `Science Fiction` as it is.
- **What the file says is added to what's there:** a color, description, or `applies_to` in the file replaces the tag's, other names are added, and anything the file leaves out stays as it is. To clear a color or remove another name, use the tag manager.
- **Problems are left out, and the rest imports:** a color that isn't `#rrggbb`, a kind of item this keep doesn't have, a sub-tag that applies to more than its parent allows. A file that isn't YAML imports nothing.
- **Delete tags not in this file that no item uses** (off unless you tick it) also deletes the tags the file doesn't mention, but only those no item has and with no sub-tag that stays. The preview lists them, and the ones kept because items use them. If you renamed a tag in the file and items use it, the old one stays: merge it into the new one with **Merge…**.

An import is one step: **Undo** puts every tag back as it was.
