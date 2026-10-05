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
- **Authors** are related in order (an ordered relationship), so the first and last author
  stay first and last; the user can reorder them by hand.
"""

import difflib
import json
import logging
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tagalot.themes.api import (
    BIBLIOGRAPHY_EXTENSIONS,
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
    contains,
    field,
    find_identifiers,
    pdf_text,
    read_bibliography,
    read_pdf_info,
    related,
    role,
    split_people,
    top_values,
)

logger = logging.getLogger(__name__)

PAPER_EXTENSIONS = frozenset({".pdf"})

SIDECAR, EXPORT, DOCUMENT, NAMED, BARE = 40, 30, 20, 10, 0
"""How much a source of a paper's details is trusted (the best giving a value wins): a
sidecar bibliography, a library export, a PDF's document info (and text), a named file
(Zotero's pattern), a bare file name. A literature note (#321) will rank above them."""
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
    extensions = PAPER_EXTENSIONS | BIBLIOGRAPHY_EXTENSIONS
    read_last = BIBLIOGRAPHY_EXTENSIONS  # exports meet the PDFs read in the same scan
    entities = [Author, Venue, Paper]
    containment = [contains(Venue, Paper)]
    relationships = [
        related("authors", Author, Paper, label="Papers", reverse_label="Authors", ordered=True)
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
        for resource in batch:
            details = ctx.prepared(resource)
            if not details:
                continue
            if resource.ext in BIBLIOGRAPHY_EXTENSIONS:
                self._ingest_bibliography(resource, dict(details), ctx)
            else:
                self._ingest_paper(resource, dict(details), ctx)

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
            for key in ("doi", "arxiv", "pmid"):
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
        for key in ("doi", "arxiv", "pmid"):
            value = entry.get(key)
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
