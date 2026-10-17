# Keep configuration

**Keep → Configure keep…** opens a window of four tabs. Every change is saved as you make it (to `keep.toml`, or to your own settings for **On this computer** and **In all your keeps**); a value that can't be used is explained under the form.

![Keep configuration's Keep tab, for a research keep](../images/screens/keep-configuration.png)

## Folders

The folders the keep watches, each with its state (online, offline, not watched, not scanned yet). For the selected folder:

- **Name** and **Folder** (its path, exactly as you type it; a network share keeps its form).
- **On this computer:** a different path for this computer only (see [Keeps and folders](keeps-and-folders.md#on-this-computer)), with Browse and Clear.
- **Skip:** patterns for files to leave out, one per line, each with an optional note after ` # `. See [Skipping files](keeps-and-folders.md#skipping-files).
- **Write back:** "Let Write to file… change files in this folder", off unless you turn it on (it asks first). See [Writing to files](write-to-file.md).
- **Assets folder:** where its Markdown files' pictures and links are looked for when they aren't beside the file (an attachments folder): a folder from the top of this folder, or from each file's own folder when it starts with `./`. Empty uses the keep's (shown greyed). A file's own `assets` key comes first. See [Assets folders](../themes/books.md#assets-folders).
- **Status:** how many files and folders it knows, how many are offline or missing, the last scan, and the last error.
- **Theme options for this folder:** the theme's settings for this folder only, over the keep's (tick one to override it here).
- **Scan now** (this folder), **Stop watching** / **Watch again**, and **Remove…**.

**Add folder…** watches another folder. Adding a folder, watching one again, or changing a path or Skip patterns offers to scan.

## Thumbnails

- **Largest size:** the theme's size, or one of the keep's own (16 to 2048 pixels). Bigger looks sharper when you zoom in, and takes more space. Thumbnails at the old size are made again as they're shown.
- **Make new and changed items' thumbnails after each scan:** on by default. Turn it off for a huge keep on a slow share; thumbnails are then made only as pages show them.
- **Stored:** how many thumbnails are stored and their size, with **Clear…** (also **Keep → Clear thumbnail cache…**). They're made again as you browse.

## Keep

- **Name** (editable), the keep's folder, its theme (and its version, and whether it's built in), and what kinds of items it holds.
- **Assets folder:** the assets folder of every watched folder that doesn't name its own (see Folders above). Covers are looked for there at the next scan; notes' pictures and links at once.
- **See-through folders:** folder names, one per line, such as `! Posters`. A folder with one of these names is never an item, but nothing in it is ignored: its files count as its parent folder's. A poster in `Heat (1995)/! Posters` is Heat's poster, and pictures in `3rd Birthday/! Posters` are among the `3rd Birthday` Folder's. Names match exactly, ignoring case. They're saved when you leave the box, and the next scan reads the keep's files again. If the change would remove Folders you've tagged, Tagalot asks first, since their tags go with them.
- **Theme options:** the theme's settings for this keep, each with **Default**. They apply at the next scan, which reads the affected files again. The built-in ones: 2D assets' **Artist folder level** and books' **Source folder level** (see each [theme](../themes/README.md)).
- **Online details** (themes that look things up): **Look up details online**, and what it would send. See [Online details](online-details.md).
- **Search inside documents** (themes with documents): **Off**, **Words**, or **Substrings**, what's stored, and **Clear contents text…**. See [Searching inside documents](search-inside-documents.md).

## Notes

**Notes folder:** where notes for items without a folder of their own live (see [The notes folder](notes.md#the-notes-folder)). **Choose…** picks it: a folder inside a watched folder is used in place; one outside every watched folder is watched for notes only, after asking. **None** stops using it. Either applies at the next scan, which offers to start.

**Let Save as note… add new notes to this folder:** off by default. Turn it on (it asks first) to let **Save as note…** on an item write its [typed note](notes.md#save-as-note) as a new file here. That's all it lets Tagalot do: add new notes, never change or replace a file, even when the watched folder the notes folder is in doesn't allow **Write to file…**. Choosing another notes folder turns it off. **New notes go** in a folder for each kind of item (`Actors/Keanu Reeves.md`), or all in the notes folder, typed by `tagalot-type`.

**Keys to ignore in notes:** front-matter keys that never become Extra fields, one per line. **In this keep** is saved with the keep; **In all your keeps** is saved for you on this computer; **Always** lists the built-in ones. Each list is saved when you leave its box and applies at the next scan. See [Notes](notes.md#keys-to-ignore).
