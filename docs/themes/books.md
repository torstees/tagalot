# Books

For ebooks, comics, stories, and documents: **books** and **comics**, their **authors**, and the **series**, **universes**, and **collections** they belong to.

## What it makes

- **Book:** an EPUB, PDF, Markdown file, Word (`.docx`, and `.doc` by name), OpenDocument (`.odt`), Pages file, or a **link file** (`.url`, `.webloc`, `.desktop`) for a work that lives on the web (a serial on Royal Road or AO3; opening it opens the page).
- **Comic:** a CBZ, CBR, or CB7, with its `ComicInfo.xml`: series, number, volume, story arc, writers and artists, publisher, imprint.
- **Author:** one per spelling the files give ("Pratchett, Terry" as its sort name). [Merge](../guide/dedupe.md) two spellings to make them one; later files under either land on the merged author.
- **Series** (in reading order), **Universe** (series and works sharing a world: Discworld, Cosmere), and **Collection** (an omnibus or anthology).
- **Credits:** a book's page shows **Written by** and **Art by**; an author's page their **Books**, **Comics**, **Illustrated**, and **Comic art**. Add and remove credits by hand; they stay.

**One work, several files:** the same book as an EPUB and a PDF, or bought from two stores, is one book whose files are its versions. While a book has one file, what the file says is what the book says; with several, each file only adds.

What each format gives is in [File formats](../reference/file-formats.md). Without details, the file name is read: `Author - Title (Year)`, `Series 03 - Title`, comics as `Series #012 (2020)`.

## Searches

**Books** (by author, series, and number) and **Comics** (by series, volume, and number), grids that inherit tags (tag a series and its books match); **Authors** (by sort name), **Series** (each opening to its works in reading order), **Universes**, and **Collections**.

## Sources

Where a file came from. The **Source folder level** option names the folder that is the store (`1` for `Humble Bundle/…`, `2` for `Ebooks/Kobo/…`; `0`, the default, for none); else front matter's `source`, a comic's web address, or an EPUB's publisher. A book's **Sources** field lists its files' sources.

## Covers and thumbnails

An EPUB shows its cover, a PDF its first page, a Word or OpenDocument file its preview, a comic its first page. A Markdown file's front matter can name a cover picture (`cover: images/cover.jpg`, relative to the file, or Obsidian's `[[cover.jpg]]`). A series, universe, or collection shows one of its first works'.

## Genres and subjects as tags

An EPUB's subjects, a comic's genres and tags, a PDF's keywords, and Markdown's `tags` are [file keywords](../guide/file-keywords.md).

## Searching inside books

Books are documents: when the keep [searches inside documents](../guide/search-inside-documents.md), the text of their EPUBs, PDFs, Markdown, Word, and OpenDocument files is read, and **In documents** finds books by what they say. Comics aren't (their pages are pictures).

## Online details

A book with an ISBN can be looked up in Open Library (with your consent, [Online details](../guide/online-details.md)), filling in an empty year, publisher, language, and description. What the files say always wins; titles, authors, and series never come from it.

## Writing to Markdown

**Write to file…** can write a Markdown book's tags and the details you edited into its front matter, in folders you allow. See [Writing to files](../guide/write-to-file.md).

## Duplicates

Books by the same first writer with nearly the same title (unless they're different numbers in a series) are listed as similar items.
