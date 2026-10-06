# Research

For papers, preprints, theses, chapters, and reports: **papers** with their **authors** in order, the **venues** they appeared in, and **projects** you make.

## What it makes

- **Paper:** its kind (article, preprint, conference paper, chapter, book, thesis, report), year, venue, volume, issue, pages, DOI, arXiv ID, PubMed ID, citation key, abstract, and link; and your own **Read** status (Unread, Reading, Read) and rating. Its files: the paper (several: the preprint and the published version, a second download), bibliography files, supplements, data, code, and notes.
- **Author:** in each paper's order (first and last author matter); move them up or down by hand on a paper's page, and later scans keep your order.
- **Venue:** a journal or conference, holding its papers (newest first).
- **Project:** a reading list you make: **New project…** in the Projects search, **Add to project…** on papers, **Remove from …** on its page. Tag a project and, with Inherit tags, its papers match.

**One paper, several files:** papers are known by their identifiers (DOI, else arXiv ID, else PubMed ID, else title and first author), so a preprint and its published version, or two downloads, are one paper with two versions.

## Where the details come from

Every file describing a paper is a **source**, and each detail comes from the best source that gives it:

1. **your edits** (always);
2. a **literature note**: a Markdown file whose front matter names the paper's `citekey`, `doi`, `arxiv`, or `pmid` (linked as its notes; its `tags` are file keywords);
3. a **sidecar bibliography**: a `.bib`, `.ris`, or CSL `.json` named like the PDF beside it;
4. a **library export**: any other bibliography file (Zotero, including Better BibTeX's auto-export, Mendeley, JabRef). Its entries are matched to papers by the file paths they name, else by DOI, arXiv ID, or PubMed ID; entries matching no paper are ignored;
5. **online details** from Crossref or arXiv, if you allow lookups ([Online details](../guide/online-details.md));
6. the **PDF**: its document info, and the DOI, arXiv ID, or PubMed ID on its first page (junk titles like "Microsoft Word - draft3.docx" are ignored);
7. the **file name**: Zotero's `Author et al. - 2020 - Title.pdf`, or an arXiv download `2101.01234v2.pdf`.

Bibliography files and notes are read after the PDFs in each scan, so they always meet the papers they describe, whichever arrived first.

## Commands

On selected papers (right-click, or a paper's page):

- **Copy citation:** a plain citation of each (`Vaswani, A., Shazeer, N., & Parmar, N. (2017). Title. Venue…`).
- **Export BibTeX…:** saves the papers as a `.bib` file where you choose. Saving inside a watched folder asks first, never replaces a file there, and adds the new file to that folder's Skip list (so the keep doesn't read its own export back).
- **Open DOI page:** the paper at doi.org, else its arXiv page, else its link, in your browser.

## Citations by hand

A paper's page has **Cites** and **Cited by** (one direction), and **Related** (both ways), each with **Add…**. A cited paper that isn't in the keep can be added by name.

## Searches

**Papers** (a list, newest first), **Authors** (by sort name), **Venues**, and **Projects**. A reading list by status is a saved search (Papers with `Read: Unread`).

## Searching inside papers

Papers are documents: when the keep [searches inside documents](../guide/search-inside-documents.md), their PDFs' text is read page by page, and **In documents** finds papers by what they say, showing the page that matched.

## Dashboard and duplicates

Papers by year and top venues. Papers sharing a DOI or arXiv ID, or by the same first author with similar titles, are listed as similar items.
