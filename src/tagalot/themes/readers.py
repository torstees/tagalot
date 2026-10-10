"""Reading book and document formats, for themes (DESIGN.md §9 books, "Reading files").

These are public helpers, re-exported by :mod:`tagalot.themes.api`, so every theme reads a
format the same way (the books theme first, research next). They only read, never write;
they return plain values (``None`` or empty for what a file doesn't say); and they raise
``OSError`` or ``ValueError`` for a file that can't be read at all, which a theme's
``prepare`` catches and reports. Loading this module loads only the standard library; a
reader that needs more (PDFium for PDFs, PyYAML for YAML front matter, the core's archive
reader for comics) imports it when called. PDFium isn't thread-safe, and ``prepare`` runs on
several threads, so every PDF call holds :data:`PDFIUM`.
"""

import datetime
import html
import io
import plistlib
import posixpath
import re
import threading
import tomllib
import zipfile
from typing import Any
from xml.etree import ElementTree

MAX_XML = 2 * 1024 * 1024
"""Package and metadata documents larger than this aren't read (a corrupt or hostile file)."""
MAX_COVER = 20 * 1024 * 1024
"""A cover image larger than this isn't read."""
MAX_FRONT_MATTER = 1024 * 1024
"""How much of a Markdown file is read for its front matter and first heading."""
PDFIUM = threading.Lock()
"""Held for every call into PDFium, which isn't thread-safe (DESIGN.md §6, #292)."""

_DC = "{http://purl.org/dc/elements/1.1/}"
_OPF = "{http://www.idpf.org/2007/opf}"
_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_ROLES = {"aut": "writer", "ill": "artist", "art": "artist", "edt": "editor", "trl": "translator"}


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _xml(data: bytes) -> ElementTree.Element:
    if len(data) > MAX_XML:
        raise ValueError("metadata too large")
    return ElementTree.fromstring(data)


def _text(element: ElementTree.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    return " ".join(element.text.split()) or None


def _plain(text: str | None) -> str | None:
    """Text without the HTML that EPUB descriptions often hold."""
    if text is None:
        return None
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split()) or None


def _year(text: str | None) -> int | None:
    found = re.match(r"\s*(\d{4})", text or "")
    return int(found.group(1)) if found else None


def _number(text: str | None) -> float | None:
    try:
        return float(text) if text not in (None, "") else None
    except ValueError:
        return None


# --- EPUB ---


def _opf_path(book: zipfile.ZipFile) -> str:
    """Where the package document is, from ``META-INF/container.xml`` (else the first .opf)."""
    try:
        container = _xml(book.read("META-INF/container.xml"))
        rootfile = container.find(f".//{_CONTAINER}rootfile")
        if rootfile is not None and rootfile.get("full-path"):
            return str(rootfile.get("full-path"))
    except KeyError:
        pass
    for name in book.namelist():
        if name.lower().endswith(".opf"):
            return name
    raise ValueError("no package document (.opf)")


def read_epub(path: str) -> dict[str, Any]:
    """An EPUB's metadata (EPUB 2 and 3):

    ``title``; ``creators`` (``(name, role)`` pairs in order: ``writer``, ``artist``,
    ``editor``, ``translator``, or ``None`` for no role, which most books mean as writer);
    ``series`` and ``series_index`` (Calibre's meta, or EPUB 3 ``belongs-to-collection`` of
    type series); ``collections`` (other ``belongs-to-collection`` names, as sets);
    ``subjects`` (keywords); ``publisher``; ``language``; ``year``; ``isbn``;
    ``description``; ``source`` (``dc:source``, often the web address it came from); and
    ``cover`` (the cover image's path inside the file, or ``None``).
    """
    with zipfile.ZipFile(path) as book:
        opf_path = _opf_path(book)
        package = _xml(book.read(opf_path))
    meta = package.find(f"{_OPF}metadata")
    if meta is None:
        meta = package.find("metadata")
    if meta is None:
        meta = ElementTree.Element("metadata")
    refines: dict[str, dict[str, str]] = {}  # EPUB 3: <meta refines="#id" property="role">
    for element in meta:
        if _local(element.tag) == "meta" and element.get("refines") and element.get("property"):
            target = str(element.get("refines")).lstrip("#")
            refines.setdefault(target, {})[str(element.get("property"))] = _text(element) or ""

    creators: list[tuple[str, str | None]] = []
    for element in meta.findall(f"{_DC}creator"):
        name = _text(element)
        if not name:
            continue
        code = element.get(f"{_OPF}role") or refines.get(element.get("id") or "", {}).get("role")
        creators.append((name, _ROLES.get(code or "") if code else None))

    series, series_index = None, None
    collections: list[str] = []
    for element in meta:
        if _local(element.tag) != "meta":
            continue
        name, content = element.get("name"), element.get("content")
        if name == "calibre:series" and content:
            series = " ".join(content.split())
        elif name == "calibre:series_index":
            series_index = _number(content)
        elif element.get("property") == "belongs-to-collection" and _text(element):
            facts = refines.get(element.get("id") or "", {})
            if facts.get("collection-type") == "series" and series is None:
                series = _text(element)
                series_index = _number(facts.get("group-position")) or series_index
            elif facts.get("collection-type") != "series":
                collections.append(str(_text(element)))

    isbn = None
    for element in meta.findall(f"{_DC}identifier"):
        value = _text(element) or ""
        digits = re.sub(r"[^0-9Xx]", "", value)
        scheme = (element.get(f"{_OPF}scheme") or "").lower()
        if (scheme == "isbn" or "isbn" in value.lower()) and len(digits) in (10, 13):
            isbn = digits.upper()
            break
    date = _text(meta.find(f"{_DC}date"))
    return {
        "title": _text(meta.find(f"{_DC}title")),
        "creators": creators,
        "series": series,
        "series_index": series_index,
        "collections": collections,
        "subjects": [s for e in meta.findall(f"{_DC}subject") if (s := _text(e))],
        "publisher": _text(meta.find(f"{_DC}publisher")),
        "language": _text(meta.find(f"{_DC}language")),
        "year": _year(date),
        "isbn": isbn,
        "description": _plain(_text(meta.find(f"{_DC}description"))),
        "source": _text(meta.find(f"{_DC}source")),
        "cover": _cover_href(package, meta, opf_path),
    }


def _cover_href(
    package: ElementTree.Element, meta: ElementTree.Element, opf_path: str
) -> str | None:
    """The cover image's path in the zip: the manifest item with ``properties="cover-image"``
    (EPUB 3), else the one ``<meta name="cover">`` names (EPUB 2)."""
    manifest = package.find(f"{_OPF}manifest")
    if manifest is None:
        manifest = package.find("manifest")
    if manifest is None:
        return None
    items = [e for e in manifest if _local(e.tag) == "item"]
    chosen = next((e for e in items if "cover-image" in (e.get("properties") or "").split()), None)
    if chosen is None:
        cover_id = next(
            (
                e.get("content")
                for e in meta
                if _local(e.tag) == "meta" and e.get("name") == "cover"
            ),
            None,
        )
        chosen = next((e for e in items if cover_id and e.get("id") == cover_id), None)
    href = chosen.get("href") if chosen is not None else None
    if not href:
        return None
    return posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), href))


def epub_cover(path: str) -> bytes | None:
    """The bytes of an EPUB's cover image, or ``None`` if it names none (or it's too big)."""
    href = read_epub(path)["cover"]
    if href is None:
        return None
    with zipfile.ZipFile(path) as book:
        try:
            info = book.getinfo(href)
        except KeyError:
            return None
        if info.file_size > MAX_COVER:
            return None
        return book.read(info)


# --- comic archives ---


def _comic_info_bytes(path: str) -> bytes | None:
    """The bytes of a comic archive's ``ComicInfo.xml`` (any folder, any case), or ``None``.
    The archive is recognized by content (a ``.cbr`` that is really a zip still works); RAR
    needs an external tool, as for thumbnails (DESIGN.md §10 "Archives")."""
    # The core's reader, imported here: the core imports this package's api.
    from tagalot.core.thumbnails.archive import SOLID_READ_BUDGET, NoRarTool, open_archive

    with open_archive(path) as archive:
        members = archive.members()
        for position, member in enumerate(members):
            if member.is_dir or posixpath.basename(member.name).lower() != "comicinfo.xml":
                continue
            if member.encrypted or member.size > MAX_XML:
                return None
            if archive.solid and sum(m.size for m in members[:position]) > SOLID_READ_BUDGET:
                return None  # too deep in a solid archive to be worth unpacking
            try:
                return archive.read(member, MAX_XML)
            except NoRarTool:
                return None
    return None


def _people(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def read_comic_info(path: str) -> dict[str, Any] | None:
    """A comic archive's ``ComicInfo.xml`` (the ComicRack / Anansi schema), or ``None`` if it
    has none: ``series``, ``number`` (as written: "12", "1.5", "Annual 1"), ``volume``,
    ``title``, ``writers``, ``artists`` (pencils, inks, colors, covers), ``publisher``,
    ``imprint``, ``genres`` and ``tags`` (keywords), ``web``, ``year``, ``story_arc``,
    ``series_group`` (a universe), and ``summary``."""
    data = _comic_info_bytes(path)
    if data is None:
        return None
    root = _xml(data)
    field = {_local(e.tag).lower(): _text(e) for e in root}
    artists: list[str] = []
    for key in ("penciller", "inker", "colorist", "coverartist"):
        artists += [p for p in _people(field.get(key)) if p not in artists]
    return {
        "series": field.get("series"),
        "number": field.get("number"),
        "volume": int(v) if (v := field.get("volume") or "").isdigit() else None,
        "title": field.get("title"),
        "writers": _people(field.get("writer")),
        "artists": artists,
        "publisher": field.get("publisher"),
        "imprint": field.get("imprint"),
        "genres": _people(field.get("genre")),
        "tags": _people(field.get("tags")),
        "web": field.get("web"),
        "year": _year(field.get("year")),
        "story_arc": field.get("storyarc"),
        "series_group": field.get("seriesgroup"),
        "summary": field.get("summary"),
    }


# --- names and lists, as documents write them ---


def split_people(value: Any) -> list[str]:
    """People named in one value: a list, or text joined by ``;``, ``&``, or ``and``
    (``"Terry Pratchett; Neil Gaiman"``), or commas. Commas split a part only when every
    piece looks like a full name, so ``"Pratchett, Terry"`` stays one person and ``"Ann
    Leckie, Martha Wells, and N. K. Jemisin"`` is three."""
    if isinstance(value, list | tuple):
        return [n for v in value for n in split_people(v)]
    if not isinstance(value, str):
        return []
    people: list[str] = []
    for part in re.split(r"\s*(?:;|&|\band\b)\s*", value):
        pieces = [p.strip() for p in part.split(",") if p.strip()]
        if len(pieces) > 1 and all(" " in p for p in pieces):
            people += pieces
        elif pieces:
            people.append(", ".join(pieces))  # one name, without a serial comma's leftover
    return [" ".join(p.split()) for p in people]


def split_keywords(value: Any, spaces: bool = True) -> list[str]:
    """Keywords in one value: a list, or text split on commas and semicolons (else, with
    ``spaces``, on spaces, as Obsidian's ``tags: fantasy reading``; without, a phrase such
    as a PDF's ``Language models`` is one keyword); a leading ``#`` is dropped, and a
    nested tag keeps its path (``Genre/Fantasy``)."""
    if isinstance(value, list | tuple):
        words = [w for v in value for w in split_keywords(v, spaces)]
    elif isinstance(value, str):
        listed = re.search(r"[,;]", value) or not spaces
        pieces = re.split(r"[,;]", value) if listed else value.split()
        words = [" ".join(p.strip().lstrip("#").split()) for p in pieces]
    elif isinstance(value, int | float) and not isinstance(value, bool):
        words = [str(value)]
    else:
        words = []
    found: dict[str, str] = {}
    for word in words:
        if word and word.casefold() not in found:
            found[word.casefold()] = word
    return list(found.values())


# --- PDF ---


def _pdf_date(text: str | None) -> int | None:
    """``D:20200131…`` → 2020."""
    return _year((text or "").removeprefix("D:"))


def read_pdf_info(path: str) -> dict[str, Any]:
    """A PDF's document information: ``title``, ``authors`` (from ``Author``, split as
    :func:`split_people` does), ``subject``, ``keywords`` (from ``Keywords``, split as
    :func:`split_keywords` does), ``year`` (of ``CreationDate``), and ``pages``. Raises
    ``ValueError`` for a file PDFium can't open (damaged, or locked with a password)."""
    import pypdfium2

    with PDFIUM:
        try:
            pdf = pypdfium2.PdfDocument(path)
        except pypdfium2.PdfiumError as e:
            raise ValueError(f"not a PDF PDFium can open: {e}") from e
        try:
            meta = pdf.get_metadata_dict(skip_empty=True)
            pages = len(pdf)
        finally:
            pdf.close()

    def text(key: str) -> str | None:
        value = meta.get(key)
        return " ".join(value.split()) or None if isinstance(value, str) else None

    return {
        "title": text("Title"),
        "authors": split_people(text("Author")),
        "subject": text("Subject"),
        "keywords": split_keywords(text("Keywords"), spaces=False),
        "year": _pdf_date(text("CreationDate")),
        "pages": pages,
    }


MAX_PDF_TEXT = 20_000
"""How much of a PDF's text :func:`pdf_text` returns."""


def pdf_text(path: str, pages: int = 1, limit: int = MAX_PDF_TEXT) -> str:
    """The text of a PDF's first ``pages`` pages (PDFium's reading of it; a scanned PDF
    without a text layer has none), at most ``limit`` characters. Raises ``ValueError`` as
    :func:`read_pdf_info`."""
    import pypdfium2

    found: list[str] = []
    with PDFIUM:
        try:
            pdf = pypdfium2.PdfDocument(path)
        except pypdfium2.PdfiumError as e:
            raise ValueError(f"not a PDF PDFium can open: {e}") from e
        try:
            for index in range(min(pages, len(pdf))):
                page = pdf[index]
                try:
                    text = page.get_textpage()
                    try:
                        found.append(text.get_text_range())
                    finally:
                        text.close()
                finally:
                    page.close()
                if sum(len(t) for t in found) >= limit:
                    break
        finally:
            pdf.close()
    return "\n".join(found).replace("\r\n", "\n")[:limit]


_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)
_ARXIV = re.compile(
    r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)"
    r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})(v\d+)?",
    re.IGNORECASE,
)
_ARXIV_NAME = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$")
_PMID = re.compile(r"\bPMID\s*:?\s*(\d{5,9})\b", re.IGNORECASE)


def find_identifiers(text: str) -> dict[str, str | None]:
    """The first DOI, arXiv ID, and PubMed ID in ``text`` (a paper's first page): ``doi``
    (lowercase, without ``https://doi.org/``, trailing punctuation dropped), ``arxiv``
    (``2101.01234`` or ``hep-th/9901001``, without its version), ``arxiv_version``
    (``"v2"``), and ``pmid``; ``None`` for each one not found. An arXiv DOI
    (``10.48550/arXiv.2101.01234``) gives the arXiv ID, not a DOI. A bare arXiv file name
    (``2101.01234v2``) counts too."""
    found: dict[str, str | None] = {"doi": None, "arxiv": None, "arxiv_version": None}
    found["pmid"] = None
    for match in _DOI.finditer(text):
        doi = match.group(1).rstrip(".,;:)]}'\"").lower()
        arxiv = _ARXIV_DOI.match(doi)
        if arxiv:
            found["arxiv"] = found["arxiv"] or arxiv.group(1)
            continue
        found["doi"] = doi
        break
    arxiv_id = _ARXIV.search(text) or _ARXIV_NAME.match(text.strip())
    if arxiv_id:
        found["arxiv"] = found["arxiv"] or arxiv_id.group(1).lower()
        found["arxiv_version"] = arxiv_id.group(2)
    pmid = _PMID.search(text)
    if pmid:
        found["pmid"] = pmid.group(1)
    return found


def pdf_cover(path: str, size: int = 512) -> bytes | None:
    """The first page of a PDF drawn as a JPEG whose longer side is about ``size`` pixels,
    or ``None`` for a PDF without pages. Raises ``ValueError`` as :func:`read_pdf_info`."""
    import pypdfium2

    with PDFIUM:
        try:
            pdf = pypdfium2.PdfDocument(path)
        except pypdfium2.PdfiumError as e:
            raise ValueError(f"not a PDF PDFium can open: {e}") from e
        try:
            if len(pdf) == 0:
                return None
            page = pdf[0]
            try:
                width, height = page.get_size()
                scale = max(0.05, min(size / max(width, height, 1.0), 8.0))
                image = page.render(scale=scale).to_pil()
            finally:
                page.close()
        finally:
            pdf.close()
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


# --- Markdown front matter ---


def _front_matter(text: str) -> tuple[dict[str, Any] | None, str]:
    """The front matter block of a Markdown text (YAML between ``---`` lines, TOML between
    ``+++`` lines) parsed, and the text after it."""
    lines = text.split("\n")
    marker = lines[0].strip() if lines else ""
    if marker not in ("---", "+++"):
        return None, text
    ends = ("---", "...") if marker == "---" else ("+++",)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ends), None)
    if end is None:
        return None, text  # an opening line only: a thematic break, not front matter
    block, body = "\n".join(lines[1:end]), "\n".join(lines[end + 1 :])
    if marker == "+++":
        try:
            return tomllib.loads(block), body
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"TOML front matter: {e}") from e
    import yaml

    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML front matter: {e}") from e
    if data is None:
        return {}, body
    if not isinstance(data, dict):
        raise ValueError("YAML front matter isn't a set of keys and values")
    return data, body


def split_front_matter(text: str) -> tuple[dict[str, Any] | None, str]:
    """A Markdown text's front matter, parsed (``None`` when it has none), and the text
    after it; ``ValueError`` for front matter that doesn't parse. For the core's notes
    (§9 *Notes*); not part of the theme API."""
    return _front_matter(text)


def _scalar_year(value: Any) -> int | None:
    if isinstance(value, datetime.date):  # a datetime is a date too
        return value.year
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10000:
        return value
    return _year(value) if isinstance(value, str) else None


def read_front_matter(path: str) -> dict[str, Any]:
    """A Markdown file's front matter (YAML between ``---`` lines, or TOML between ``+++``
    lines) and first heading:

    ``title`` (``title``, else the first ``# heading``); ``authors`` (``authors`` or
    ``author``); ``series`` and ``series_index`` (``series_index``, ``number``, or
    ``series_number``); ``universe``; ``keywords`` (``tags`` and ``keywords``, split as
    :func:`split_keywords` does); ``source``; ``link`` (``url`` or ``link``, if a web
    address); ``year`` (``year``, else ``date``); ``publisher``; ``language`` (or
    ``lang``); ``description`` (or ``summary``); ``cover`` (a picture's path, as written,
    relative to the file); and ``fields``, every key and value as written, for a theme's own
    keys. Keys are matched ignoring case. A file without front matter gives just its
    heading. Raises ``ValueError`` for front matter that doesn't parse.
    """
    with open(path, "rb") as file:
        raw = file.read(MAX_FRONT_MATTER)
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    data, body = _front_matter(text)
    fields = {str(k): v for k, v in (data or {}).items()}
    key = {k.casefold(): v for k, v in fields.items()}

    def first(*names: str) -> Any:
        return next((key[n] for n in names if key.get(n) not in (None, "", [])), None)

    def words(*names: str) -> str | None:
        value = first(*names)
        if isinstance(value, bool) or value is None or isinstance(value, dict | list):
            return None
        return " ".join(str(value).split()) or None

    heading = next(
        (m.group(1).strip() for m in re.finditer(r"^#\s+(.+?)\s*#*\s*$", body, re.MULTILINE)),
        None,
    )
    index = first("series_index", "number", "series_number")
    link = words("url", "link")
    return {
        "title": words("title") or heading,
        "authors": split_people(first("authors", "author")),
        "series": words("series"),
        "series_index": _number(str(index)) if index is not None else None,
        "universe": words("universe"),
        "keywords": split_keywords(
            [*split_keywords(key.get("tags")), *split_keywords(key.get("keywords"))]
        ),
        "source": words("source"),
        "link": link if link and re.match(r"https?://", link, re.IGNORECASE) else None,
        "year": _scalar_year(first("year")) or _scalar_year(first("date")),
        "publisher": words("publisher"),
        "language": words("language", "lang"),
        "description": words("description", "summary"),
        "cover": words("cover", "image"),
        "fields": fields,
    }


# --- office documents: Word (DOCX), OpenDocument (ODT), Pages ---

_CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
_DCTERMS = "{http://purl.org/dc/terms/}"
_ODF_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_ODF_META = "{urn:oasis:names:tc:opendocument:xmlns:meta:1.0}"

OFFICE_PREVIEWS = (
    "docProps/thumbnail.jpeg",
    "docProps/thumbnail.jpg",
    "docProps/thumbnail.png",
    "Thumbnails/thumbnail.png",
    "preview.jpg",
    "QuickLook/Thumbnail.jpg",
    "preview-web.jpg",
    "preview-micro.jpg",
)
"""Where office documents keep a picture of their first page, in order of preference: Word's
(when saved with one), OpenDocument's (always), and Pages' (a Pages file saved as one file,
which is a zip; a Pages package folder isn't read)."""


def _open_zip(path: str) -> zipfile.ZipFile:
    """Open a zip-based document; ``ValueError`` if it isn't one."""
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a zip-based document: {e}") from e


def _member(doc: zipfile.ZipFile, name: str, limit: int) -> bytes | None:
    """A zip member by name, ignoring case, if it isn't larger than ``limit``."""
    wanted = name.casefold()
    for info in doc.infolist():
        if info.filename.casefold() == wanted:
            return None if info.file_size > limit else doc.read(info)
    return None


def read_office_info(path: str) -> dict[str, Any]:
    """An office document's properties: ``title``, ``authors``, ``subject``, ``keywords``,
    ``description``, ``year`` (when it was created), ``language``, and ``format``
    (``"docx"``, ``"odt"``, or ``None`` for a zip with neither, such as a Pages file, whose
    own format holds nothing readable: its title is its file name).

    Word documents keep them in ``docProps/core.xml``; OpenDocument ones in ``meta.xml``,
    whose initial creator is the author (its creator is whoever saved it last). Raises
    ``ValueError`` for a file that isn't a zip, as a legacy ``.doc`` isn't: read nothing
    from those.
    """
    found: dict[str, Any] = {
        "title": None,
        "authors": [],
        "subject": None,
        "keywords": [],
        "description": None,
        "year": None,
        "language": None,
        "format": None,
    }
    with _open_zip(path) as doc:
        core = _member(doc, "docProps/core.xml", MAX_XML)
        meta = None if core is not None else _member(doc, "meta.xml", MAX_XML)
    if core is not None:
        root = _xml(core)
        found.update(
            format="docx",
            title=_text(root.find(f"{_DC}title")),
            authors=split_people(_text(root.find(f"{_DC}creator"))),
            subject=_text(root.find(f"{_DC}subject")),
            keywords=split_keywords(_text(root.find(f"{_CP}keywords")), spaces=False),
            description=_text(root.find(f"{_DC}description")),
            year=_year(_text(root.find(f"{_DCTERMS}created"))),
            language=_text(root.find(f"{_DC}language")),
        )
    elif meta is not None:
        office = _xml(meta).find(f"{_ODF_OFFICE}meta")
        if office is None:
            office = ElementTree.Element("meta")
        author = _text(office.find(f"{_ODF_META}initial-creator")) or _text(
            office.find(f"{_DC}creator")
        )
        words = [w for e in office.findall(f"{_ODF_META}keyword") if (w := _text(e))]
        found.update(
            format="odt",
            title=_text(office.find(f"{_DC}title")),
            authors=split_people(author),
            subject=_text(office.find(f"{_DC}subject")),
            keywords=split_keywords(words, spaces=False),
            description=_text(office.find(f"{_DC}description")),
            year=_year(_text(office.find(f"{_ODF_META}creation-date"))),
            language=_text(office.find(f"{_DC}language")),
        )
    return found


def office_cover(path: str) -> bytes | None:
    """The picture of its first page an office document keeps (Word's, OpenDocument's, or
    Pages' preview; see :data:`OFFICE_PREVIEWS`), or ``None``."""
    with _open_zip(path) as doc:
        for name in OFFICE_PREVIEWS:
            data = _member(doc, name, MAX_COVER)
            if data:
                return data
    return None


# --- link files: Windows .url, macOS .webloc, Linux .desktop ---

MAX_LINK_FILE = 64 * 1024
"""Link files are tiny; larger ones aren't read."""


def _ini_section(text: str, section: str) -> dict[str, str]:
    """The ``key=value`` lines of one ``[section]`` of an INI-style file, keys lowercase
    (the first of a repeated key wins)."""
    found: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            inside = line[1:-1].strip().casefold() == section.casefold()
        elif inside and "=" in line and not line.startswith(("#", ";")):
            key, _, value = line.partition("=")
            found.setdefault(key.strip().casefold(), value.strip())
    return found


def read_link_file(path: str) -> dict[str, Any]:
    """A link file's ``url`` and ``title`` (``None`` when it gives none; the file name is
    the usual title): a Windows Internet shortcut (``.url``: ``URL=`` under
    ``[InternetShortcut]``), a macOS ``.webloc`` (a property list's ``URL``, XML or binary),
    or a Linux ``.desktop`` entry of ``Type=Link`` (``URL=`` and ``Name=``; other types,
    such as applications, give no ``url``). Tagalot never visits the address. Raises
    ``ValueError`` for another extension or a file that can't be read as one."""
    ext = posixpath.splitext(path.lower())[1]
    with open(path, "rb") as file:
        raw = file.read(MAX_LINK_FILE + 1)
    if len(raw) > MAX_LINK_FILE:
        raise ValueError("too large for a link file")
    if ext == ".webloc":
        try:
            plist = plistlib.loads(raw)
        except (plistlib.InvalidFileException, ValueError, OverflowError) as e:
            raise ValueError(f"not a property list: {e}") from e
        url = plist.get("URL") if isinstance(plist, dict) else None
        return {"url": url.strip() or None if isinstance(url, str) else None, "title": None}
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    if ext == ".url":
        entry = _ini_section(text, "InternetShortcut")
        return {"url": entry.get("url") or None, "title": None}
    if ext == ".desktop":
        entry = _ini_section(text, "Desktop Entry")
        if entry.get("type", "").casefold() != "link":
            return {"url": None, "title": entry.get("name") or None}
        return {"url": entry.get("url") or None, "title": entry.get("name") or None}
    raise ValueError(f"not a link file: {ext or 'no extension'}")
