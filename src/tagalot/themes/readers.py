"""Reading book and document formats, for themes (DESIGN.md §9 books, "Reading files").

These are public helpers, re-exported by :mod:`tagalot.themes.api`, so every theme reads a
format the same way (the books theme first, research next). They only read, never write;
they return plain values (``None`` or empty for what a file doesn't say); and they raise
``OSError`` or ``ValueError`` for a file that can't be read at all, which a theme's
``prepare`` catches and reports. They use only the standard library, except where a format
reads archives as thumbnails do.
"""

import html
import posixpath
import re
import zipfile
from typing import Any
from xml.etree import ElementTree

MAX_XML = 2 * 1024 * 1024
"""Package and metadata documents larger than this aren't read (a corrupt or hostile file)."""
MAX_COVER = 20 * 1024 * 1024
"""A cover image larger than this isn't read."""

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
