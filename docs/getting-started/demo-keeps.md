# The demo keeps

Tagalot can make ready-made keeps with small sample files, scanned and tagged, so you can try everything without your own collection. They need a source checkout ([from source](install.md#from-source)).

```bash
uv run python scripts/make_demo_keep.py --reset
uv run tagalot scratch/Demo.keep
```

`--reset` makes the keep afresh (deleting the previous demo keep and its sample files, both under `scratch/`). Each theme has its own:

| Keep | Make it with | Or with [just](https://just.systems) |
|---|---|---|
| `scratch/Demo.keep` (Files) | `make_demo_keep.py --reset` | `just demo` |
| `scratch/Assets.keep` (2D assets) | `make_demo_keep.py --assets --reset` | `just demo-assets` |
| `scratch/Music.keep` | `make_demo_keep.py --music --reset` | `just demo-music` |
| `scratch/Movies.keep` | `make_demo_keep.py --movies --reset` | `just demo-movies` |
| `scratch/Books.keep` | `make_demo_keep.py --books --reset` | `just demo-books` |
| `scratch/Research.keep` | `make_demo_keep.py --research --reset` | `just demo-research` |
| `scratch/Media.keep` (a tiny theme of its own) | `make_demo_keep.py --media --reset` | `just demo-media` |

Then open one with `uv run tagalot scratch/<Name>.keep` (the `just` recipes open it for you).

## What's in them

- **Demo:** a few images and documents in nested folders, names with accents and spaces, and operating-system leftovers (`.DS_Store`, `._sunset.jpg`, `Thumbs.db`) that the default Skip patterns leave out; a small tag tree (with an alias) on most files.
- **2D assets:** two artists' images (one with a `folder.jpg`), a font, a zip and a 7z archive of images, and a picture with no artist.
- **Music:** short silent MP3s with tags: an album with a cover and a song in two versions (MP3 and FLAC), an album with embedded cover art, a two-disc album, a compilation, an untagged folder, and a song directly in the folder.
- **Movies:** movies in their own folders and loose, with posters, screenshots, `.nfo` files with casts, a collection, an actor's photo, and extras.
- **Books:** EPUBs, PDFs, Markdown, Word and OpenDocument files, link files, and comic archives in folders named for where they came from (Kobo, Humble Bundle, Comixology, Royal Road). One book is in two stores, two series are in reading order, a comic series is in a universe, and one book is a near-duplicate of another. Genres come from the files (two match no tag yet). Searching inside documents is on.
- **Research:** PDFs with DOIs and arXiv IDs: one paper downloaded twice, a preprint and its published version, a junk title ("Microsoft Word - …"), and a PDF with no details that a sidecar `.bib` names. A Zotero-style library export gives the papers their venues, and a literature note names its paper by citation key. Searching inside documents is on.
- **Media:** artists, albums, and songs from a small theme the script installs in your themes folder (`tagalot_demo_media.py`; delete it when you're done), to show searches over several kinds of item.

The sample files live under `scratch/` (for example `scratch/music-files`), which git ignores.
