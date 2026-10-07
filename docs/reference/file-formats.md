# File formats

What each theme reads from each kind of file. Tagalot only ever reads them.

## Pictures, archives, fonts

| Format | Read |
|---|---|
| JPEG, PNG, GIF, WebP, BMP, TIFF, PSD, TGA | size (width × height); thumbnails, turned upright by EXIF orientation |
| ZIP, 7z, RAR (and CBZ, CB7, CBR) | the list of files (how many images); the thumbnail is the image named like a cover, preview, or thumb, else the first in natural order (`page2` before `page10`) |
| TTF, OTF, WOFF, WOFF2 | family and style; a drawn sample as the thumbnail |

Reading an image from a RAR needs a RAR tool (`unrar`, 7-Zip, or `bsdtar`); without one, RAR archives show an icon. A folder picture (`folder.jpg`, `cover.png`, `front.jpg`, `AlbumArt_…_Large.jpg`, `AlbumArtSmall.jpg`, as JPEG, PNG, or WebP) is used for albums and artists.

## Music

MP3, FLAC, Ogg Vorbis, Opus, M4A/AAC, WAV, WMA, AIFF, through their tags: title, track and disc numbers, artist, album artist, album, year, genre (several), length, bitrate, and embedded cover art (ID3, FLAC, MP4, and Ogg pictures; the front cover first).

## Video

MKV, MP4, AVI, MOV, WMV, WebM, M4V, MPG, through MediaInfo: length, resolution, codec, and audio languages. Names give the title and year. `.nfo` files (Kodi's XML): title, year, plot, genre, director, length, rating, cast in order, and set.

## Books and documents

| Format | Read |
|---|---|
| **EPUB** | title, creators with their roles (writer, illustrator, editor, translator), series (Calibre's, or EPUB 3's), collections, subjects (as keywords), publisher, language, date, ISBN, description, source, and the cover |
| **Kindle** (`.mobi`, `.azw`, `.azw3`) | title, authors, publisher, description, ISBN, subjects (as keywords), date, language, and the cover. A book with DRM still gives these; only its text is protected. (Not `.kfx`.) |
| **PDF** | document info: title, author, subject (as the description), keywords, creation year; the first page as the cover. Research also reads the first page's text for a DOI, arXiv ID, or PubMed ID |
| **Markdown** (`.md`, `.markdown`) | YAML (`---`) or TOML (`+++`) front matter: `title` (else the first `#` heading), `author`/`authors`, `series`, `series_index` (or `number`), `universe`, `tags`/`keywords`, `source`, `url`/`link`, `date`/`year`, `publisher`, `language`, `description`/`summary`, `cover`; research's notes use `citekey`, `doi`, `arxiv`, `pmid` |
| **Word** (`.docx`) and **OpenDocument** (`.odt`) | title, author, keywords, subject, description, created, language, and the preview picture |
| **Pages** | the preview picture (the title is the file name) |
| **`.doc`** | the file name only |
| **Link files** (`.url`, `.webloc`, `.desktop` links) | the web address |
| **Comic archives** | `ComicInfo.xml`: series, number, volume, title, writer, penciller, inker, colorist, cover artist, publisher, imprint, genre and tags (as keywords), web, year, story arc, series group (as the universe) |

## Bibliographies (research)

| Format | Read |
|---|---|
| **BibTeX / BibLaTeX** (`.bib`) | entries with braced or quoted values, `@string` macros, LaTeX accents, `and`-separated names; the file paths Zotero, JabRef, Mendeley, and Better BibTeX write |
| **RIS** (`.ris`) | entries, with `L1`–`L4` file links |
| **CSL-JSON** (`.json`) | CSL items (other JSON files are ignored) |

Each gives: type, citation key, title, authors and editors in order, year, venue, volume, issue, pages, publisher, DOI, arXiv ID, PubMed ID, URL, abstract, keywords, and file paths.

## Searching inside documents

PDFs (page by page), EPUBs and Kindle books (chapter by chapter; not a Kindle book with DRM), and Markdown, plain text, Word (`.docx`), and OpenDocument (`.odt`) files, up to 5 MB of text each and 2,000 PDF pages. See [Searching inside documents](../guide/search-inside-documents.md).
