"""Research theme: papers, preprints, theses, and reports, and their authors in order
(DESIGN.md §9, research).

```
Author ↔ Paper (authors, in order)
```

- **A paper and its files:** a paper is keyed by identifier: DOI, else arXiv ID (without
  its version), else PubMed ID, else its title and first author. A file giving any
  identifier the keep already knows joins that paper, so a preprint and its published
  version are one paper with two versions, and duplicate downloads collapse. A file already
  linked to a paper updates it in place; while it is the paper's one file, what it says
  replaces what it said before, and with several each only fills in.
- **Reading a PDF** (``prepare``): identifiers in its first page's text
  (:func:`~tagalot.themes.api.find_identifiers`), its document info (a junk title such as
  "Microsoft Word - draft3.docx" is ignored), else its file name: Zotero's
  ``Author et al. - 2020 - Title.pdf``, ``Author et al. (2020) Title.pdf``, or an arXiv
  download's ``2101.01234v2.pdf``.
- **Authors** are related in order (an ordered relationship), so the first and last author
  stay first and last; the user can reorder them by hand.
- Venues and reading lists (projects) come with bibliography files (#320) and by hand.
"""

import difflib
import logging
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tagalot.themes.api import (
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
    ThumbnailProvider,
    field,
    find_identifiers,
    pdf_text,
    read_pdf_info,
    related,
    role,
    split_people,
    top_values,
)

logger = logging.getLogger(__name__)

PAPER_EXTENSIONS = frozenset({".pdf"})


class Author(Entity):
    """Someone who wrote papers: one per spelling the files give (merge them to join a
    person's spellings)."""

    title_label = "Name"
    sort_name: str | None = field("Sort name", search="text")
    """``Vaswani, Ashish``: how the Authors view sorts them."""


class Paper(Entity):
    """A paper; its files (a preprint, the published version, a duplicate download) are its
    versions."""

    authors: str | None = field("Authors", card=True, search="text", editable=False)
    """Its authors' names in order, as its files give them (the credits list them)."""
    year: int | None = field("Year", card=True, search="range")
    kind: str | None = field("Kind", search="choice")
    """Article, preprint, thesis, chapter, report, book."""
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
    titled: int | None = field("Title from", editable=False, detail=False)
    """Where its title came from: 2 a document's info, 1 a named file (Zotero's
    ``Author - Year - Title``), 0 a bare file name (``1706.03762v7``). A paper's other file
    with a better one names it, whichever is read first."""
    credited: int | None = field("Authors from", editable=False, detail=False)
    """Where its authors came from, ranked as :attr:`titled` (0: none)."""
    roles = [
        role("paper", kinds={"any"}, many=True, primary=True, label="Files"),
        role("supplement", kinds={"any"}, many=True, label="Supplementary files"),
        role("data", kinds={"any"}, many=True, label="Data"),
        role("code", kinds={"any"}, many=True, label="Code"),
        role("notes", kinds={"any"}, many=True, label="Notes"),
    ]
    card_lines = ("authors", "year")


class ResearchTheme(Theme):
    """Papers and their authors, in order."""

    id, name, version, api_version = "research", "Research", 1, 4
    extensions = PAPER_EXTENSIONS
    entities = [Author, Paper]
    relationships = [
        related("authors", Author, Paper, label="Papers", reverse_label="Authors", ordered=True)
    ]
    near_duplicate_threshold = 0.9
    dashboard = [top_values("Papers by year", Paper, "year")]
    views = [
        SearchView(
            "Papers",
            [Paper],
            layout="list",
            default_sort=[SortBy("year", descending=True), SortBy("title")],
        ),
        SearchView("Authors", [Author], default_sort=[SortBy("sort_name"), SortBy("title")]),
        DetailView(
            Paper,
            [
                Section.fields(),
                Section.related("authors"),
                Section.role("paper"),
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
            if resource.kind != "file" or resource.ext not in PAPER_EXTENSIONS:
                continue
            if not resource.path:
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
            if details:
                self._ingest_paper(resource, dict(details), ctx)

    def _ingest_paper(
        self, resource: ResourceInfo, details: dict[str, Any], ctx: IngestContext
    ) -> None:
        error = details.pop("error", None)
        if error:
            ctx.warn(resource, f"Couldn't read it: {error}")
        authors: list[str] = details.pop("authors")
        keywords: list[str] = details.pop("keywords")
        title: str = details.pop("title")
        titled: int = details.pop("titled")
        credited: int = details.pop("credited")
        existing = ctx.entities_of(resource, "paper")
        if existing:
            paper = existing[0]
        else:
            paper = find_paper(ctx, details) or ctx.upsert(
                Paper, paper_key(details, title, authors, resource.relpath)
            )
            ctx.link(paper, resource, "paper")
        alone = not [r for r in ctx.linked(paper, "paper") if r != resource.id]
        before = ctx.get(paper).fields
        values: dict[str, Any] = {
            "year": details.get("year"),
            "doi": details.get("doi"),
            "arxiv": details.get("arxiv"),
            "pmid": details.get("pmid"),
            "authors": "; ".join(authors) or None,
        }
        better_authors = credited > (before.get("credited") or 0)
        if alone:
            values["kind"] = paper_kind(values["doi"], values["arxiv"])
            ctx.update(paper, title=title, titled=titled, credited=credited, **values)
        else:  # another file may say more: fill in what's missing, and better names
            known = {k: v for k, v in values.items() if v and not before.get(k)}
            doi = before.get("doi") or values["doi"]
            known["kind"] = paper_kind(doi, before.get("arxiv") or values["arxiv"])
            if better_authors:
                known.update(authors=values["authors"], credited=credited)
            if titled > (before.get("titled") or 0):
                ctx.update(paper, title=title, titled=titled, **known)
            else:
                ctx.update(paper, **known)
        ctx.keywords(paper, resource, keywords)
        if alone or better_authors:
            set_authors(paper, authors, ctx)


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
    """A paper's details from its PDF's document info and first-page ``text`` (``None``
    and ``""`` for an unreadable file), each missing one from its file name."""
    name = paper_name(relpath)
    ids = find_identifiers(text) if text else {}
    if not ids.get("arxiv") and name.get("arxiv"):
        ids = {**ids, "arxiv": name["arxiv"], "arxiv_version": name.get("arxiv_version")}
    meta = info or {}
    title: str = meta.get("title") or ""
    titled = 2 if len(title) >= 4 and not JUNK_TITLE.match(title) else 0
    if not titled:
        title, titled = name["title"], name["titled"]
    authors = [a for a in meta.get("authors") or [] if plausible_name(a)]
    credited = 2 if authors else (1 if name["authors"] else 0)
    authors = authors or name["authors"]
    year = name["year"] or arxiv_year(ids.get("arxiv"))
    return {
        "title": title,
        "titled": titled,
        "credited": credited,
        "authors": authors,
        "year": year,
        "doi": ids.get("doi"),
        "arxiv": ids.get("arxiv"),
        "pmid": ids.get("pmid"),
        "keywords": list(meta.get("keywords") or []),
    }


def paper_name(relpath: str) -> dict[str, Any]:
    """What a paper's file name says: Zotero's ``Author et al. - 2020 - Title``,
    ``Author et al. (2020) Title``, an arXiv download (``2101.01234v2``), or just a
    title."""
    stem = posixpath.splitext(relpath.rpartition("/")[2])[0].replace("_", " ").strip()
    name: dict[str, Any] = {
        "title": stem,
        "titled": 0,
        "authors": [],
        "year": None,
        "arxiv": None,
    }
    if ARXIV_NAME.match(stem):
        version = re.search(r"v\d+$", stem)
        name["arxiv"] = stem[: version.start()] if version else stem
        name["arxiv_version"] = version.group(0) if version else None
        return name
    found = ZOTERO_NAME.match(stem) or PAREN_NAME.match(stem)
    if found:
        name["titled"] = 1
        name["title"] = found.group("title").strip()
        name["year"] = int(found.group("year"))
        people = re.sub(r"\s+et\.? al\.?$", "", found.group("authors").strip())
        name["authors"] = split_people(people)
    return name


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


# --- keys, names, and authors ---


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


def paper_key(details: Mapping[str, Any], title: str, authors: Sequence[str], relpath: str) -> str:
    """A new paper's key: its first identifier, else its title and first author's surname,
    else its title within its folder."""
    for key in ("doi", "arxiv", "pmid"):
        if details.get(key):
            return f"{key}:{details[key]}"
    if authors:
        return f"title:{normalize(title)}|{surname(authors[0])}"
    return f"title:{relpath.rpartition('/')[0]}|{normalize(title)}"


def find_paper(ctx: IngestContext, details: Mapping[str, Any]) -> EntityRef | None:
    """The paper that already has one of the file's identifiers (a preprint's published
    version, a second download), if any."""
    for key in ("doi", "arxiv", "pmid"):
        value = details.get(key)
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
