# File keywords

Many files carry their own tags: an EPUB's subjects, a song's genre, a comic's genres, a PDF's keywords, a Markdown file's `tags`. Tagalot calls these **file keywords**. They become your tags only by matching tags you've made, so your tag tree never fills up with every spelling the files use.

## How a keyword becomes a tag

A keyword matches a tag when it is (ignoring case and accents):

1. the tag's full path (`Genre/Fantasy`, `Genre > Fantasy`), else
2. one of the tag's other names (aliases), else
3. the tag's name, when no other tag has that name.

A matching item gets the tag. Nothing ever creates a tag for you.

- Tags from files are shown in *italics* in the Tags panel (when every selected item has it only from its files).
- When the files change, a scan updates them: a keyword gone from every file takes its tag away, unless you also added the tag yourself.
- **Taking one off by hand sticks:** later scans leave it off. The item's page offers **Restore**.

## Keywords that match no tag

- After a scan that finds new ones, the status bar and the Activity panel say so, with a link to review them.
- TOOLS → **File keywords (N)** shows how many match no tag.
- [Triage](triage.md) has an **Unmatched keywords** list of items with such keywords, and a **Keywords** column.

The **File keywords** page lists each keyword with how many items have it and a few examples. **Show** chooses Unmatched, Matched, Ignored, or All. For the selected keywords:

- **Map to tag…** ties them to a tag you pick: each becomes that tag's alias, so it matches from now on, on every item (double-click does the same).
- **Create tag…** makes a tag for the keyword, at a path you confirm (a keyword `Science/Physics` suggests `Science > Physics`).
- **Ignore** stops listing it (**Stop ignoring** undoes that).

An item's page shows its keywords under **From the file**, and what each gives: a tag, "removed from this item" (with **Restore**), "not for this type", *Ignored*, or *No tag* (with **Map…** and **Ignore**).

Mapping, ignoring, and restoring are all steps you can undo.
