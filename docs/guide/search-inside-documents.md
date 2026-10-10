# Searching inside documents

In keeps of documents ([Books](../themes/books.md) and [Research](../themes/research.md)), Tagalot can read your documents' text and find them by what they say, not only by their details.

![Papers matching "recurrence" inside their PDFs, with where it matched](../images/screens/in-documents.png)

## Turning it on

A new keep with a theme that has documents asks **Search inside documents**: **Words** (the default), **Substrings**, or **Off**. To change it later: **Keep → Configure keep… → Keep** tab, **Search inside documents**.

- **Words** finds whole words and the starts of words (`photosynth` finds "photosynthesis"), ignoring case and accents. Its index is about the size of the text.
- **Substrings** also finds text inside words (`synth` finds "photosynthesis"). Its index is 3 to 5 times bigger.
- **Off** reads nothing and keeps no index.

Switching between Words and Substrings rebuilds the index from the text already read (no file is read again), and searches use the old index until the new one is ready. **Off** keeps the text, so turning it on again is quick; **Clear contents text…** deletes the text and the index (and turns it off).

## Reading the text

After each scan, Tagalot reads the text of new and changed documents in the background ("Reading contents: N left" in the status bar). It reads:

- **PDFs** page by page (a scanned PDF without a text layer has no text to read);
- **EPUBs** chapter by chapter;
- **Kindle books** (`.mobi`, `.azw`, `.azw3`) chapter by chapter, unless they have DRM (their text is protected);
- **Markdown**, **plain text**, **Word (.docx)**, and **OpenDocument (.odt)** files as one page each.

A file's text stops at 5 MB, and a PDF's at 2,000 pages. A file that can't be read is listed in the [Activity panel](activity.md) as **Text not read**, and isn't tried again until it changes, or until a newer Tagalot reads more kinds of files (then it's tried once more). The text is kept in the keep's `fulltext.db`.

## Searching

Tick **In documents** in the filter bar (it shows on searches that can list documents), then type in the search box:

- **Words:** every word must be on the same page; the last word can be the start of a word.
- **Substrings:** every term must be on the same page, inside words or not.

Items still match by their titles and fields as usual; the documents add more. A paper matches if any of its versions does.

## Where it matched

- In the **list**, a **Match** column after the title shows a snippet around the match, with the matched words in bold. Its tooltip names the file and the page (`p. 8`, or `ch. 3` for an EPUB or a Kindle book).
- On **grid** cards, the last line is the snippet, from the match on.
