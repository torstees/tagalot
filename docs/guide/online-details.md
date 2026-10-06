# Online details

Some themes can look an item's details up online by an identifier it has: the [Research](../themes/research.md) theme looks papers up by DOI (Crossref) or arXiv ID (arXiv), and the [Books](../themes/books.md) theme looks books up by ISBN (Open Library). Tagalot never goes online for anything else, and never without your say-so.

## Asking first

Nothing is looked up until the first time a lookup would send something. Then Tagalot asks, once for the keep, naming each service and what it would be sent: "Tagalot would send DOIs to Crossref and arXiv IDs to arXiv. Nothing else about your files leaves this computer."

- **Allow for this keep:** look things up from now on, starting with everything already in the keep.
- **Not now:** send nothing; you'll be asked again the next time you open the keep (or use Look up online).
- **Never:** don't look anything up. Look up online then offers to turn it on.

Change your answer in **Keep → Configure keep… → Keep** tab: **Look up details online**. The answer is saved in the keep's `keep.toml`.

## When

- **After each scan,** for the items it added or changed, in the background ("Looking up online: N left").
- **Look up online** on selected items (right-click) or on an item's page (More ▾) looks them up again now.

Answers are kept in the keep, so each identifier is fetched once (Look up online fetches again). Requests to a service are spaced out (arXiv asks for one every three seconds). When you're offline, or a service is down, nothing changes, the Activity panel says why, and the items are tried again later.

## What wins

Looked-up details fill in and correct what Tagalot guessed from a file, but never override your files' own curated details or your edits:

- **Research:** a literature note, a sidecar bibliography, or a library export beats what Crossref or arXiv say; what they say beats what Tagalot read from the PDF or its file name.
- **Books:** Open Library only fills in empty fields (year, publisher, language, description); what the files say always wins.
- **Your edits win over everything.** Looked-up values are marked as such, and scans and lookups never change a value you edited.

Lookups only ever fill in descriptive details, never identifiers, so they can't change which item a file belongs to.
