"""Research theme: papers, preprints, theses, and reports, their authors in order, and the
venues they appeared in (DESIGN.md §9, research).

```
Venue ⊃ Paper        Author ↔ Paper (authors, in order)
```

- **A paper and its files:** a paper is keyed by identifier: DOI, else arXiv ID (without
  its version), else PubMed ID, else its title and first author. A file giving any
  identifier the keep already knows joins that paper, so a preprint and its published
  version are one paper with two versions, and duplicate downloads collapse.
- **Sources:** each file that describes a paper (its PDFs, a bibliography beside one, a
  library export) is kept as a source of the paper's details, in the hidden ``origins``
  field. Each field takes the value of the best source that gives one: a sidecar
  bibliography, then a library export, then the PDF's document info, then a named file,
  then a bare file name (the user's edits win over all, as always). A source that changes
  replaces only what it gave; one gone, or no longer naming the paper, lets go of it.
- **Reading a PDF** (``prepare``): identifiers in its first page's text, its document
  info (junk titles ignored), else its file name: Zotero's ``Author et al. - 2020 -
  Title.pdf``, ``Author et al. (2020) Title.pdf``, or an arXiv download's
  ``2101.01234v2.pdf``.
- **Bibliography files** (``.bib``, ``.ris``, CSL-JSON ``.json``): one named like a PDF
  beside it (a **sidecar**) describes that paper; any other is a **library export**
  (Zotero, Better BibTeX, Mendeley, JabRef), whose entries are matched to papers by the
  file paths they name (the end of the path, as in Zotero's ``storage/KEY/name.pdf``),
  else by DOI, arXiv ID, or PubMed ID. Entries matching no paper are ignored. They are
  read last in a scan (``read_last``), and again when other files of their folder are
  read, so they meet PDFs added later.
- **Literature notes:** a Markdown file whose front matter names a ``citekey``, ``doi``,
  ``arxiv``, or ``pmid`` is linked to that paper as its notes; what it says outranks every
  file but the user's edits, and its ``tags`` are the paper's file keywords. Notes are read
  last too, after the bibliographies whose citation keys they use.
- **Authors** are related in order (an ordered relationship), so the first and last author
  stay first and last; the user can reorder them by hand.
- **Citations by hand:** a paper's page has **Cites** and **Cited by** (one relationship of
  Paper with itself, with a direction) and **Related** (symmetric).
- **Actions:** **Copy citation** (one plain style), **Export BibTeX…** (the user saves it),
  and **Open DOI page** (doi.org, else arXiv), for the selected papers.
"""

import difflib
import json
import logging
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, ClassVar

from tagalot.themes.api import (
    BIBLIOGRAPHY_EXTENSIONS,
    ActionContext,
    DetailView,
    Entity,
    EntityRef,
    Icon,
    ImageFile,
    IngestContext,
    Record,
    ResourceInfo,
    SearchView,
    Section,
    SortBy,
    Theme,
    ThumbnailContext,
    ThumbnailProvider,
    action,
    contains,
    field,
    find_identifiers,
    pdf_text,
    read_bibliography,
    read_front_matter,
    read_pdf_info,
    related,
    role,
    split_people,
    top_values,
)

logger = logging.getLogger(__name__)

PAPER_EXTENSIONS = frozenset({".pdf"})
NOTE_EXTENSIONS = frozenset({".md", ".markdown"})

NOTE, SIDECAR, EXPORT, DOCUMENT, NAMED, BARE = 50, 40, 30, 20, 10, 0
"""How much a source of a paper's details is trusted (the best giving a value wins): a
literature note, a sidecar bibliography, a library export, a PDF's document info (and
text), a named file (Zotero's pattern), a bare file name."""
MERGED = (
    "title", "authors", "year", "kind", "venue", "volume", "issue", "pages", "doi", "arxiv",
    "pmid", "citekey", "abstract", "url",
)  # fmt: skip
"""The details a paper's sources give."""


class Author(Entity):
    """Someone who wrote papers: one per spelling the files give (merge them to join a
    person's spellings)."""

    title_label = "Name"
    sort_name: str | None = field("Sort name", search="text")
    """``Vaswani, Ashish``: how the Authors view sorts them."""


class Venue(Entity):
    """A journal, conference, or book series, holding the papers that appeared in it."""

    contents_sort = (SortBy("year", descending=True), SortBy("title"))


class Paper(Entity):
    """A paper; its files (a preprint, the published version, a duplicate download) are its
    versions."""

    authors: str | None = field("Authors", card=True, search="text", editable=False)
    """Its authors' names in order, as its sources give them (the credits list them)."""
    year: int | None = field("Year", card=True, search="range")
    kind: str | None = field("Kind", search="choice")
    """Article, preprint, conference paper, chapter, book, thesis, report, web page."""
    venue: str | None = field("Venue", search="choice")
    volume: str | None = field("Volume")
    issue: str | None = field("Issue")
    pages: str | None = field("Pages")
    doi: str | None = field("DOI", search="text")
    arxiv: str | None = field("arXiv ID", search="text")
    pmid: str | None = field("PubMed ID", search="text")
    citekey: str | None = field("Citation key", search="text")
    abstract: str | None = field("Abstract", search="text")
    url: str | None = field("Link", search="text", display="url")
    read_status: str | None = field("Read", search="choice")
    """The user's: Unread, Reading, Read."""
    rating: int | None = field("Rating", search="range")
    origins: str | None = field("Sources", editable=False, detail=False)
    """What each of its sources says, as JSON: ``{resource id: {field: [rank, value]}}``."""
    roles = [
        role("paper", kinds={"any"}, many=True, primary=True, label="Files"),
        role("bibliography", kinds={"any"}, many=True, label="Bibliography files"),
        role("supplement", kinds={"any"}, many=True, label="Supplementary files"),
        role("data", kinds={"any"}, many=True, label="Data"),
        role("code", kinds={"any"}, many=True, label="Code"),
        role("notes", kinds={"any"}, many=True, label="Notes"),
    ]
    card_lines = ("authors", "year")


class ContentsThumbnail(ThumbnailProvider):
    """A venue shows one of its first papers' first pages."""

    id = "research.contents"
    tried = 5

    def candidates(self, entity: EntityRef, ctx: ThumbnailContext) -> Iterable[ResourceInfo]:
        for child in ctx.children(entity)[: self.tried]:
            found = ctx.thumbnail_of(child)
            if found is not None:
                yield found


class ResearchTheme(Theme):
    """Papers, their authors in order, and their venues."""

    id, name, version, api_version = "research", "Research", 1, 4
    extensions = PAPER_EXTENSIONS | BIBLIOGRAPHY_EXTENSIONS | NOTE_EXTENSIONS
    # Bibliographies meet the PDFs read in the same scan, then notes meet both (a note's
    # citation key comes from a bibliography).
    read_last: ClassVar[Sequence[str]] = (".bib", ".ris", ".json", ".md", ".markdown")
    entities = [Author, Venue, Paper]
    containment = [contains(Venue, Paper)]
    relationships = [
        related("authors", Author, Paper, label="Papers", reverse_label="Authors", ordered=True),
        related("cites", Paper, Paper, label="Cites", reverse_label="Cited by"),
        related("related", Paper, Paper, label="Related", symmetric=True),
    ]
    near_duplicate_threshold = 0.9
    dashboard = [
        top_values("Papers by year", Paper, "year"),
        top_values("Top venues", Paper, "venue"),
    ]
    views = [
        SearchView(
            "Papers",
            [Paper],
            layout="list",
            default_sort=[SortBy("year", descending=True), SortBy("title")],
        ),
        SearchView("Authors", [Author], default_sort=[SortBy("sort_name"), SortBy("title")]),
        SearchView("Venues", [Venue], inherit_tags=True),
        DetailView(
            Paper,
            [
                Section.fields(),
                Section.related("authors"),
                Section.related("cites"),
                Section.related("related"),
                Section.role("paper"),
                Section.role("bibliography"),
                Section.role("supplement"),
                Section.role("data"),
                Section.role("code"),
                Section.role("notes"),
            ],
        ),
        DetailView(Author, [Section.fields(), Section.related("authors")]),
    ]

    # --- actions ---

    @action("Copy citation", [Paper])
    def copy_citation(self, papers: Sequence[EntityRef], ctx: ActionContext) -> None:
        """Put the papers' citations on the clipboard, one per paragraph."""
        text = "\n\n".join(citation(ctx.get(p), authors_of(p, ctx)) for p in papers)
        ctx.copy_text(text)
        count = "1 citation" if len(papers) == 1 else f"{len(papers)} citations"
        ctx.message(f"Copied {count}.")

    @action("Export BibTeX\u2026", [Paper])
    def export_bibtex(self, papers: Sequence[EntityRef], ctx: ActionContext) -> None:
        """Offer to save the papers as a BibTeX file."""
        keys: set[str] = set()
        entries = [bibtex_entry(ctx.get(p), authors_of(p, ctx), keys) for p in papers]
        name = "papers.bib" if len(papers) > 1 else f"{next(iter(keys), 'paper')}.bib"
        ctx.save_text(name, "\n".join(entries))

    @action("Open DOI page", [Paper])
    def open_doi_page(self, papers: Sequence[EntityRef], ctx: ActionContext) -> None:
        """Open each paper's page: its DOI at doi.org, else its arXiv page, else its link."""
        opened = 0
        for paper in papers[:MAX_PAGES]:
            url = paper_url(ctx.get(paper).fields)
            if url:
                ctx.open_url(url)
                opened += 1
        if not opened:
            ctx.message("No DOI, arXiv ID, or link to open.")
        elif len(papers) > MAX_PAGES:
            ctx.message(f"Opened the first {MAX_PAGES} papers' pages.")

    def thumbnail_chain(self, entity_type: type[Entity]) -> Sequence[ThumbnailProvider]:
        if entity_type is Paper:
            return [ImageFile(), Icon("file")]  # its first page
        if entity_type is Venue:
            return [ContentsThumbnail(), Icon("entity")]
        return super().thumbnail_chain(entity_type)

    # --- near-duplicates ---

    def blocking_keys(self, entity_type: type[Entity], record: Record) -> Iterable[str]:
        """Papers sharing an identifier, or a first author."""
        if entity_type is not Paper:
            return ()
        keys = [f"{k}:{v}" for k in ("doi", "arxiv", "pmid") if (v := record.fields.get(k))]
        first = (record.fields.get("authors") or "").split(";")[0]
        if surname(first):
            keys.append(f"author:{surname(first)}")
        return keys

    def similarity(self, entity_type: type[Entity], a: Record, b: Record) -> float:
        """1 for papers sharing a DOI, arXiv ID, or PubMed ID; 0 for papers with different
        DOIs; else how alike their titles are."""
        for key in ("doi", "arxiv", "pmid"):
            first, second = a.fields.get(key), b.fields.get(key)
            if first and second and first == second:
                return 1.0
        if a.fields.get("doi") and b.fields.get("doi"):
            return 0.0
        return difflib.SequenceMatcher(None, normalize(a.title), normalize(b.title)).ratio()

    # --- reading files (scan worker) ---

    def prepare(self, batch: Sequence[ResourceInfo]) -> Mapping[int, Any]:
        found: dict[int, Any] = {}
        for resource in batch:
            if resource.kind != "file" or not resource.path:
                continue
            if resource.ext in BIBLIOGRAPHY_EXTENSIONS:
                try:
                    found[resource.id] = {"entries": read_bibliography(resource.path)}
                except Exception as e:  # reported; the papers keep what it said before
                    found[resource.id] = {"error": f"{type(e).__name__}: {e}"}
                continue
            if resource.ext in NOTE_EXTENSIONS:
                try:
                    note = note_details(read_front_matter(resource.path))
                except Exception as e:  # front matter that doesn't parse: no note
                    logger.info("Not a literature note: %s (%s)", resource.relpath, e)
                    note = None
                found[resource.id] = {"note": note}
                continue
            if resource.ext not in PAPER_EXTENSIONS:
                continue
            try:
                info = read_pdf_info(resource.path)
                text = pdf_text(resource.path)
                found[resource.id] = paper_details(resource.relpath, info, text)
            except Exception as e:  # a bad file is still a paper, named from its file
                found[resource.id] = paper_details(resource.relpath, None, "")
                found[resource.id]["error"] = f"{type(e).__name__}: {e}"
        return found

    # --- ingest (DB writer) ---

    def ingest(self, batch: Sequence[ResourceInfo], ctx: IngestContext) -> None:
        notes: PaperIndex | None = None  # shared by a batch's notes; papers change otherwise
        for resource in batch:
            details = ctx.prepared(resource)
            if not details:
                continue
            if resource.ext in NOTE_EXTENSIONS:
                notes = notes or PaperIndex(ctx)
                self._ingest_note(resource, details.get("note"), notes, ctx)
                continue
            notes = None
            if resource.ext in BIBLIOGRAPHY_EXTENSIONS:
                self._ingest_bibliography(resource, dict(details), ctx)
            else:
                self._ingest_paper(resource, dict(details), ctx)

    def _ingest_note(
        self,
        resource: ResourceInfo,
        note: Mapping[str, Any] | None,
        index: "PaperIndex",
        ctx: IngestContext,
    ) -> None:
        """Link a literature note to the paper it names, as one of the paper's sources (or
        let go of the paper it no longer names)."""
        paper = index.by_ids(note["ids"]) if note else None
        for old in ctx.entities_of(resource, "notes"):
            if paper is None or old.id != paper.id:
                ctx.unlink(old, resource, "notes")
                apply_source(old, resource.id, None, ctx)
        if paper is None or note is None:
            return
        ctx.link(paper, resource, "notes")
        apply_source(paper, resource.id, note["values"], ctx)
        ctx.keywords(paper, resource, note["keywords"])

    def _ingest_paper(
        self, resource: ResourceInfo, details: dict[str, Any], ctx: IngestContext
    ) -> None:
        error = details.pop("error", None)
        if error:
            ctx.warn(resource, f"Couldn't read it: {error}")
        existing = ctx.entities_of(resource, "paper")
        if existing:
            paper = existing[0]
        else:
            title, authors = details["title"][1], details["authors"][1]
            ids = {k: details[k][1] for k in ("doi", "arxiv", "pmid")}
            found = find_paper(ctx, ids)
            paper = found or ctx.upsert(Paper, paper_key(ids, title, authors, resource.relpath))
            ctx.link(paper, resource, "paper")
        keywords = details.pop("keywords")
        apply_source(paper, resource.id, details, ctx)
        ctx.keywords(paper, resource, keywords)

    def _ingest_bibliography(
        self, resource: ResourceInfo, details: dict[str, Any], ctx: IngestContext
    ) -> None:
        if details.get("error"):
            ctx.warn(resource, f"Couldn't read it: {details['error']}")
            return
        entries: list[dict[str, Any]] = details["entries"]
        stem = posixpath.splitext(resource.relpath)[0]
        sidecar = ctx.resource_at(resource, f"{stem}.pdf") if entries else None
        matched: dict[int, tuple[EntityRef, dict[str, Any]]] = {}
        if sidecar is not None:
            papers = ctx.entities_of(sidecar, "paper")
            if papers:
                entry = sidecar_entry(entries, sidecar.relpath)
                matched[papers[0].id] = (papers[0], source_values(entry, SIDECAR))
        else:
            index = PaperIndex(ctx)
            for entry in entries:
                found = index.match(entry, resource)
                if found is not None and found.id not in matched:
                    matched[found.id] = (found, source_values(entry, EXPORT))
        for paper, values in matched.values():
            ctx.link(paper, resource, "bibliography")
            keywords = values.pop("keywords")
            apply_source(paper, resource.id, values, ctx)
            ctx.keywords(paper, resource, keywords)
        for paper in ctx.entities_of(resource, "bibliography"):
            if paper.id not in matched:  # its entry is gone: what it said goes too
                ctx.unlink(paper, resource, "bibliography")
                apply_source(paper, resource.id, None, ctx)


# --- what a file says ---

JUNK_TITLE = re.compile(
    r"^(microsoft (word|powerpoint)|untitled|document\d*$|.*\.(docx?|pdf|tex|dvi|ps|indd)$)",
    re.IGNORECASE,
)
"""Document titles that are an editor's leftovers, not a paper's title."""
ZOTERO_NAME = re.compile(r"^(?P<authors>.+?) - (?P<year>\d{4}) - (?P<title>.+)$")
"""``Vaswani et al. - 2017 - Attention Is All You Need``."""
PAREN_NAME = re.compile(r"^(?P<authors>.+?) \((?P<year>\d{4})\)\s*[-.:]?\s*(?P<title>.+)$")
"""``Vaswani et al. (2017) Attention Is All You Need``."""
ARXIV_NAME = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")


def paper_details(relpath: str, info: Mapping[str, Any] | None, text: str) -> dict[str, Any]:
    """What a PDF says about its paper, as ``{field: (rank, value)}`` (and ``keywords``):
    its document info and first-page ``text`` (``None`` and ``""`` for an unreadable file),
    each missing one from its file name, ranked by where it came from."""
    name = paper_name(relpath)
    ids = find_identifiers(text) if text else {}
    meta = info or {}
    title: str = meta.get("title") or ""
    if len(title) >= 4 and not JUNK_TITLE.match(title):
        titled = (DOCUMENT, title)
    else:
        titled = (NAMED if name["named"] else BARE, name["title"])
    authors = [a for a in meta.get("authors") or [] if plausible_name(a)]
    credited = (DOCUMENT, authors) if authors else (NAMED, name["authors"])
    arxiv = ids.get("arxiv") or name["arxiv"]
    year = name["year"] or arxiv_year(arxiv)
    return {
        "title": titled,
        "authors": credited,
        "year": (NAMED, year),
        "doi": (DOCUMENT, ids.get("doi")),
        "arxiv": (DOCUMENT, arxiv),
        "pmid": (DOCUMENT, ids.get("pmid")),
        "keywords": list(meta.get("keywords") or []),
    }


def note_details(front: Mapping[str, Any]) -> dict[str, Any] | None:
    """What a literature note's front matter (:func:`read_front_matter`) says, or ``None``
    when it names no paper: its identifiers (``citekey``, ``doi``, ``arxiv``, ``pmid``),
    the paper's details it gives (``title`` only from front matter, not a heading, which
    titles the note), ranked as a note, and its ``tags`` as keywords."""
    fields = {str(k).casefold(): v for k, v in (front.get("fields") or {}).items()}

    def text(*names: str) -> str | None:
        for name in names:
            value = fields.get(name)
            if value not in (None, "", []) and not isinstance(value, dict | list):
                return str(value).strip()
        return None

    citekey = (text("citekey", "citationkey", "citation-key") or "").lstrip("@") or None
    doi = text("doi")
    arxiv = text("arxiv", "arxiv_id", "arxivid")
    pmid = text("pmid")
    ids = {
        "citekey": citekey,
        "doi": find_identifiers(doi)["doi"] if doi else None,
        "arxiv": find_identifiers(f"arXiv:{arxiv}")["arxiv"] or arxiv if arxiv else None,
        "pmid": pmid if pmid and pmid.isdigit() else None,
    }
    if not any(ids.values()):
        return None
    values: dict[str, Any] = {
        "title": (NOTE, text("title")),
        "authors": (NOTE, front.get("authors") or []),
        "year": (NOTE, front.get("year")),
        "venue": (NOTE, text("journal", "venue", "booktitle")),
        "url": (NOTE, front.get("link")),
        **{key: (NOTE, value) for key, value in ids.items()},
    }
    return {"ids": ids, "values": values, "keywords": list(front.get("keywords") or [])}


def paper_name(relpath: str) -> dict[str, Any]:
    """What a paper's file name says: Zotero's ``Author et al. - 2020 - Title``,
    ``Author et al. (2020) Title``, an arXiv download (``2101.01234v2``), or just a
    title."""
    stem = posixpath.splitext(relpath.rpartition("/")[2])[0].replace("_", " ").strip()
    name: dict[str, Any] = {
        "title": stem,
        "named": False,
        "authors": [],
        "year": None,
        "arxiv": None,
    }
    if ARXIV_NAME.match(stem):
        version = re.search(r"v\d+$", stem)
        name["arxiv"] = stem[: version.start()] if version else stem
        return name
    found = ZOTERO_NAME.match(stem) or PAREN_NAME.match(stem)
    if found:
        name["named"] = True
        name["title"] = found.group("title").strip()
        name["year"] = int(found.group("year"))
        people = re.sub(r"\s+et\.? al\.?$", "", found.group("authors").strip())
        name["authors"] = split_people(people)
    return name


def source_values(entry: Mapping[str, Any], rank: int) -> dict[str, Any]:
    """A bibliography entry as a source of a paper's details (and its ``keywords``)."""
    values: dict[str, Any] = {name: (rank, entry.get(name)) for name in MERGED}
    values["kind"] = (rank, entry.get("kind") if entry.get("kind") != "other" else None)
    values["keywords"] = list(entry.get("keywords") or [])
    return values


def sidecar_entry(entries: Sequence[Mapping[str, Any]], pdf: str) -> Mapping[str, Any]:
    """The entry of a sidecar bibliography that describes its PDF: the one naming the PDF's
    file, else the first."""
    name = pdf.rpartition("/")[2].casefold()
    for entry in entries:
        if any(path_name(f).casefold() == name for f in entry.get("files") or []):
            return entry
    return entries[0]


def path_name(path: str) -> str:
    return path.replace("\\", "/").rpartition("/")[2]


def plausible_name(name: str) -> bool:
    """Whether a PDF's author is a person's name, not a login (``jsmith``, ``admin``)."""
    return " " in name.strip() or "," in name or any(c.isupper() for c in name[1:])


def arxiv_year(arxiv: str | None) -> int | None:
    """The year an arXiv ID was given: ``1706.03762`` is 2017."""
    found = re.match(r"^(\d{2})(\d{2})\.", arxiv or "")
    return 2000 + int(found.group(1)) if found else None


def paper_kind(doi: str | None, arxiv: str | None) -> str | None:
    """A preprint while only arXiv knows it; an article once it has a DOI."""
    if doi:
        return "article"
    return "preprint" if arxiv else None


# --- citations and BibTeX ---

MAX_PAGES = 10
"""Open DOI page opens at most this many pages at once."""
BIBTEX_TYPES = {
    "article": "article",
    "preprint": "misc",
    "conference paper": "inproceedings",
    "chapter": "incollection",
    "book": "book",
    "thesis": "phdthesis",
    "report": "techreport",
    "web page": "online",
}
"""A paper's kind as a BibTeX entry type (others: ``misc``)."""


def authors_of(paper: EntityRef, ctx: IngestContext) -> list[str]:
    """The paper's authors' names, in order."""
    return [ctx.get(person).title for person in ctx.related("authors", paper)]


def initials(name: str) -> str:
    """``Ashish Vaswani`` → ``Vaswani, A.``; ``Vaswani, Ashish`` likewise."""
    if "," in name:
        last, _, first = name.partition(",")
    else:
        first, _, last = name.strip().rpartition(" ")
    letters = " ".join(f"{part[0]}." for part in first.replace("-", " ").split() if part)
    return f"{last.strip()}, {letters}" if letters else last.strip()


def citation(record: Record, authors: Sequence[str]) -> str:
    """One plain citation: ``Vaswani, A., Shazeer, N. (2017). Title. Venue, 30(2), 1-11.
    https://doi.org/…``."""
    fields = record.fields
    names = [initials(a) for a in authors]
    who = ", ".join(names[:-1]) + (", & " if len(names) > 1 else "") + names[-1] if names else ""
    parts = [
        f"{who} ({fields.get('year') or 'n.d.'})." if who else f"({fields.get('year') or 'n.d.'})."
    ]
    parts.append(f"{record.title.rstrip('.')}.")
    venue = fields.get("venue")
    if venue:
        where = venue
        if fields.get("volume"):
            where += f", {fields['volume']}"
            if fields.get("issue"):
                where += f"({fields['issue']})"
        if fields.get("pages"):
            where += f", {fields['pages']}"
        parts.append(f"{where}.")
    url = paper_url(fields)
    if url:
        parts.append(url)
    return " ".join(parts)


def paper_url(fields: Mapping[str, Any]) -> str | None:
    """The paper's page: its DOI at doi.org, else its arXiv page, else its link."""
    if fields.get("doi"):
        return f"https://doi.org/{fields['doi']}"
    if fields.get("arxiv"):
        return f"https://arxiv.org/abs/{fields['arxiv']}"
    url = fields.get("url")
    return url if isinstance(url, str) and re.match(r"https?://", url, re.IGNORECASE) else None


def bibtex_value(text: str) -> str:
    """A value for a BibTeX field: braces balanced, LaTeX's special characters escaped."""
    text = re.sub(r"([&%$#_])", r"\\\1", str(text))
    if text.count("{") != text.count("}"):
        text = text.replace("{", "").replace("}", "")
    return text


def bibtex_entry(record: Record, authors: Sequence[str], keys: set[str]) -> str:
    """The paper as a BibTeX entry; its citation key, else one made from the first
    author's surname, the year, and the title's first word, unique among ``keys``."""
    fields = record.fields
    key = (
        fields.get("citekey")
        or "".join(
            normalize(part).replace(" ", "")
            for part in (
                surname(authors[0]) if authors else "",
                str(fields.get("year") or ""),
                (normalize(record.title).split() or ["paper"])[0],
            )
        )
        or "paper"
    )
    unique, n = key, 1
    while unique in keys:
        n += 1
        unique = f"{key}{chr(ord('a') + n - 2)}" if n <= 27 else f"{key}{n}"
    keys.add(unique)
    kind = BIBTEX_TYPES.get(fields.get("kind") or "", "misc")
    venue_field = {"article": "journal", "inproceedings": "booktitle", "incollection": "booktitle"}
    values: list[tuple[str, Any]] = [
        ("title", f"{{{bibtex_value(record.title)}}}"),
        ("author", " and ".join(bibtex_value(a) for a in authors)),
        ("year", fields.get("year")),
        (venue_field.get(kind, "howpublished"), fields.get("venue")),
        ("volume", fields.get("volume")),
        ("number", fields.get("issue")),
        ("pages", (fields.get("pages") or "").replace("-", "--") or None),
        ("doi", fields.get("doi")),
        ("eprint", fields.get("arxiv")),
        ("archiveprefix", "arXiv" if fields.get("arxiv") else None),
        ("pmid", fields.get("pmid")),
        ("url", fields.get("url")),
        ("abstract", fields.get("abstract")),
    ]
    lines = [f"@{kind}{{{unique},"]
    for name, value in values:
        if value not in (None, "", "{}"):
            text = value if name == "title" else bibtex_value(str(value))
            lines.append(f"  {name} = {{{text}}},")
    lines.append("}")
    return "\n".join(lines) + "\n"


# --- a paper's sources ---


def apply_source(
    paper: EntityRef, source: int, values: Mapping[str, Any] | None, ctx: IngestContext
) -> None:
    """Record what file ``source`` says about the paper (``{field: (rank, value)}``;
    ``None``: nothing any more), and set the paper's details from its sources: each field
    the best-ranked source's (sources no longer linked to it are dropped)."""
    record = ctx.get(paper)
    try:
        origins: dict[str, dict[str, list[Any]]] = json.loads(record.fields.get("origins") or "{}")
    except ValueError:
        origins = {}
    linked = {str(r) for r in ctx.linked(paper)}
    origins = {k: v for k, v in origins.items() if k in linked and k != str(source)}
    if values is not None:
        said = {
            name: [rank, value]
            for name, (rank, value) in values.items()
            if name in MERGED and value not in (None, "", [])
        }
        if said:
            origins[str(source)] = said
    best: dict[str, Any] = {}
    for name in MERGED:
        given = [v[name] for v in origins.values() if name in v]
        if given:
            best[name] = max(given, key=lambda pair: pair[0])[1]
    authors: list[str] = best.pop("authors", None) or []
    title = best.pop("title", None)
    best["kind"] = best.get("kind") or paper_kind(best.get("doi"), best.get("arxiv"))
    before = record.fields.get("venue")
    ctx.update(
        paper,
        title=title or None,
        origins=json.dumps(origins, sort_keys=True, ensure_ascii=False),
        authors="; ".join(authors) or None,
        **{name: best.get(name) for name in MERGED if name not in ("title", "authors")},
    )
    set_authors(paper, authors, ctx)
    set_venue(paper, best.get("venue"), before, ctx)


class PaperIndex:
    """The keep's papers by identifier, looked up once for a library export's entries."""

    def __init__(self, ctx: IngestContext) -> None:
        self.ctx = ctx
        self.by: dict[tuple[str, str], EntityRef] = {}
        for paper in ctx.find(Paper):
            fields = ctx.get(paper).fields
            for key in ("citekey", "doi", "arxiv", "pmid"):
                if fields.get(key):
                    self.by.setdefault((key, str(fields[key]).lower()), paper)

    def match(self, entry: Mapping[str, Any], export: ResourceInfo) -> EntityRef | None:
        """The paper an entry describes: by the file paths it names (the longest ending
        that is a file in the export's root), else by DOI, arXiv ID, or PubMed ID."""
        for path in entry.get("files") or []:
            parts = [p for p in path.replace("\\", "/").split("/") if p]
            for count in range(min(len(parts), 6), 0, -1):
                found = self.ctx.resource_at(export, "/".join(parts[-count:]))
                if found is not None:
                    papers = self.ctx.entities_of(found, "paper")
                    if papers:
                        return papers[0]
        return self.by_ids(entry)

    def by_ids(self, ids: Mapping[str, Any]) -> EntityRef | None:
        """The paper with one of these identifiers: DOI, arXiv ID, PubMed ID, or citation
        key, in that order."""
        for key in ("doi", "arxiv", "pmid", "citekey"):
            value = ids.get(key)
            if value and (key, str(value).lower()) in self.by:
                return self.by[(key, str(value).lower())]
        return None


# --- keys, names, authors, and venues ---


def normalize(text: str) -> str:
    """Ignore case and punctuation."""
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()


def surname(name: str) -> str:
    """``Ashish Vaswani`` and ``Vaswani, A.`` → ``vaswani``."""
    name = name.strip()
    if "," in name:
        return normalize(name.partition(",")[0])
    words = normalize(name).split()
    return words[-1] if words else ""


def paper_key(ids: Mapping[str, Any], title: str, authors: Sequence[str], relpath: str) -> str:
    """A new paper's key: its first identifier, else its title and first author's surname,
    else its title within its folder."""
    for key in ("doi", "arxiv", "pmid"):
        if ids.get(key):
            return f"{key}:{ids[key]}"
    if authors:
        return f"title:{normalize(title)}|{surname(authors[0])}"
    return f"title:{relpath.rpartition('/')[0]}|{normalize(title)}"


def find_paper(ctx: IngestContext, ids: Mapping[str, Any]) -> EntityRef | None:
    """The paper that already has one of the file's identifiers (a preprint's published
    version, a second download), if any."""
    for key in ("doi", "arxiv", "pmid"):
        value = ids.get(key)
        if value:
            found = ctx.find(Paper, **{key: value})
            if found:
                return found[0]
    return None


def sort_name(name: str) -> str:
    """``Ashish Vaswani`` → ``Vaswani, Ashish``; a name with a comma is already one."""
    if "," in name or " " not in name.strip():
        return name.strip()
    first, _, last = name.strip().rpartition(" ")
    return f"{last}, {first}"


def set_authors(paper: EntityRef, names: Sequence[str], ctx: IngestContext) -> None:
    """Credit ``names`` on the paper, in order; those it no longer names lose the credit
    (and an author left with no paper is gone)."""
    people: list[EntityRef] = []
    for name in names:
        key = normalize(name)
        if key and all(ctx.get(p).title != name for p in people):
            people.append(
                ctx.upsert(Author, f"author:{key}", title=name, sort_name=sort_name(name))
            )
    keep = {p.id for p in people}
    for person in ctx.related("authors", paper):
        if person.id not in keep:
            ctx.unrelate("authors", person, paper)
            if not ctx.related("authors", person) and not ctx.linked(person):
                ctx.delete(person)
    for position, person in enumerate(people):
        ctx.relate("authors", person, paper, position=position)


def set_venue(paper: EntityRef, name: str | None, before: str | None, ctx: IngestContext) -> None:
    """Put the paper in its venue; a venue it leaves with nothing in it is gone."""
    if before and (not name or normalize(before) != normalize(name)):
        old = ctx.upsert(Venue, f"venue:{normalize(before)}", title=before)
        ctx.uncontain(old, paper)
        if not ctx.contents(old) and not ctx.linked(old):
            ctx.delete(old)
    if name and normalize(name):
        ctx.contain(ctx.upsert(Venue, f"venue:{normalize(name)}", title=name), paper)
