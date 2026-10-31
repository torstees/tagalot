# Online details

Some themes can look an item's details up online by an identifier it has: the [Research](../themes/research.md) theme looks papers up by DOI (Crossref) or arXiv ID (arXiv), the [Books](../themes/books.md) theme looks books up by ISBN, or by title and author (Open Library), and the [Movies](../themes/movies.md#details-from-tmdb) theme looks movies up in TMDb, by TMDb or IMDb ID (with a key of your own). Tagalot never goes online for anything else, and never without your say-so.

## Asking first

Nothing is looked up until the first time a lookup would send something. Then Tagalot asks, once for the keep, naming each service and what it would be sent: "Tagalot would send DOIs to Crossref and arXiv IDs to arXiv. Nothing else about your files leaves this computer."

- **Allow for this keep:** look things up from now on, starting with everything already in the keep.
- **Not now:** send nothing; you'll be asked again the next time you open the keep (or use Look up online).
- **Never:** don't look anything up. Look up online then offers to turn it on.

Change your answer in **Keep → Configure keep… → Keep** tab: **Look up details online**. The answer is saved in the keep's `keep.toml`.

## Services that need a token

Some services need a token of your own (a free key from the service). Give it to Tagalot in **Keep → Configure keep… → Keep** tab, under **Environment**: select the variable the service needs (`TAGALOT_TMDB_TOKEN: not set (TMDb needs it)`), click **Set…**, and paste your key. It's kept in your own settings on this computer, not in the keep, so a keep you share doesn't share your key; it's never written to the log. **Show values** shows what you've set, and **Remove** forgets it.

An environment variable of the same name works too (on Windows: **Edit environment variables for your account**, then start Tagalot again); what you set in the keep wins over it. Until a token is set either way, that service isn't used or mentioned when Tagalot asks, and the Keep tab says which variable to set. Once it is, a keep that looks things up asks once about the service.

## What may identify an item

Some services can find an item that has no ID by its title, such as a home video, or an old book with no ISBN (Open Library is sent its first author too). Each keep chooses what may be sent, in **Keep → Configure keep… → Keep** tab: **Find items by** (shown for themes that can search by title):

- **IDs only** (the default): only identifiers are sent; items without one aren't looked up.
- **IDs, or a title with its year:** an item without an ID is found by its title and year.
- **IDs, or any title:** by its title alone when it has no year.

The question Tagalot asks says what would be sent with your choice. When a title finds the item, its ID is kept, and from then on it's looked up by that.

## A new service

When a newer Tagalot can use a service your keep hasn't been asked about (a covers service, say), it asks once more, about that service only: **Allow**, **Not now** (asked again the next time you open the keep), or **Don't use it**. Nothing is sent to it until you allow it, and the services you already allowed carry on either way.

## When

- **After each scan,** for the items it added or changed, in the background ("Looking up online: N left").
- **Look up online** on selected items (right-click) or on an item's page (More ▾) looks them up again now.

Answers are kept in the keep, so each identifier is fetched once (Look up online fetches again). Requests to a service are spaced out (arXiv asks for one every three seconds). When you're offline, or a service is down, nothing changes, the Activity panel says why, and the items are tried again later.

## What wins

Looked-up details fill in and correct what Tagalot guessed from a file, but never override your files' own curated details or your edits:

- **Research:** a literature note, a sidecar bibliography, or a library export beats what Crossref or arXiv say; what they say beats what Tagalot read from the PDF or its file name.
- **Books:** Open Library only fills in empty fields (year, publisher, language, description; a book found by title gets its year and description, not an edition's publisher or language); what the files say always wins. It also gives covers and authors' photos (below).
- **Your edits win over everything.** Looked-up values are marked as such, and scans and lookups never change a value you edited.

Lookups only ever fill in descriptive details, never identifiers, so they can't change which item a file belongs to.

## Pictures

A service can also have a picture of an item, such as a book's cover. Tagalot keeps it in the keep (never in your folders) and shows it **only when the item has no picture of its own**: a cover file, a picture its note names, or one you chose always wins, and adding one later replaces the online picture at once. Each picture is fetched once; **Look up online** fetches it again.
