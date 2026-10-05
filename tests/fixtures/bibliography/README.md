# Bibliography fixtures (#319)

Small exports in the shapes reference managers write them, for `tests/themes/test_bibliography.py`.

- `zotero.bib`: Zotero's own BibTeX export: tab-indented, protective braces, `month = jun` macros, an arXiv DOI, a `file` field with escaped Windows paths (`C\:\\Users\\…`) and two attachments, and a thesis with LaTeX accents (`\"{U}`, `\ss{}`, `{\O}`), an `\&`, and a braced corporate author.
- `betterbibtex.bib`: Better BibTeX's BibLaTeX auto-export: `@string` and `#` concatenation, `@comment`, `date` and `journaltitle`, `{van der Walt}`, `and others`, a DOI written as a URL, `eprinttype = {arxiv}` and `{pubmed}`, plain file paths, and a broken entry followed by one that must still be read.
- `jabref_mendeley.bib`: JabRef's `:path:PDF` file field, and Mendeley's `$\backslash$` paths and `arxivId`.
- `zotero.ris`: Zotero's RIS export: a journal article with a continued abstract line, `KW` keywords, and an `L1` `file:///` link; and a thesis.
- `library.json`: CSL-JSON as Zotero and Better BibTeX write it: `citation-key`, `non-dropping-particle`, a `literal` author, `date-parts`, and an arXiv preprint (`archive`, `number`).
