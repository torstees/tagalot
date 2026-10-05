"""Reading bibliography files: BibTeX/BibLaTeX, RIS, and CSL-JSON (DESIGN.md §9 research,
"Bibliography files"; #319).

:func:`read_bibliography` is a public helper, re-exported by :mod:`tagalot.themes.api`. It
reads what Zotero (and Better BibTeX), Mendeley, and JabRef export, and returns plain entries
with the same keys whatever the format, so a theme never sees the format. The BibTeX reader
is Tagalot's own, for the common subset (decided in review): it is written so that
bibtexparser could replace it later without changing what it returns.

Only the standard library is loaded.
"""

import json
import re
import unicodedata
from collections.abc import Iterator
from typing import Any

from tagalot.themes.readers import find_identifiers, split_keywords

MAX_BIBLIOGRAPHY = 64 * 1024 * 1024
"""Bibliography files larger than this aren't read."""

BIBLIOGRAPHY_EXTENSIONS = frozenset({".bib", ".ris", ".json"})
"""The files :func:`read_bibliography` reads (a ``.json`` file only if it is CSL-JSON)."""

KINDS: dict[str, str] = {
    "article": "article",
    "jour": "article",
    "article-journal": "article",
    "article-magazine": "article",
    "article-newspaper": "article",
    "mgzn": "article",
    "news": "article",
    "inproceedings": "conference paper",
    "conference": "conference paper",
    "conf": "conference paper",
    "cpaper": "conference paper",
    "paper-conference": "conference paper",
    "incollection": "chapter",
    "inbook": "chapter",
    "chap": "chapter",
    "chapter": "chapter",
    "book": "book",
    "mvbook": "book",
    "collection": "book",
    "phdthesis": "thesis",
    "mastersthesis": "thesis",
    "thesis": "thesis",
    "thes": "thesis",
    "techreport": "report",
    "report": "report",
    "rprt": "report",
    "unpublished": "preprint",
    "preprint": "preprint",
    "online": "web page",
    "webpage": "web page",
    "elec": "web page",
}
"""Each format's entry types, as a paper's ``kind`` (others are ``other``)."""


def read_bibliography(path: str) -> list[dict[str, Any]]:
    """The entries of a bibliography file, by its extension: BibTeX/BibLaTeX (``.bib``), RIS
    (``.ris``), or CSL-JSON (``.json``; a JSON file that isn't CSL gives no entries).

    Each entry is a dict: ``type`` (the format's own, lowercase), ``kind`` (article,
    preprint, conference paper, chapter, book, thesis, report, web page, or other),
    ``citekey``, ``title``, ``authors`` and ``editors`` (in order, as ``First Last`` names),
    ``year``, ``venue`` (the journal, proceedings, or book it is in), ``volume``, ``issue``,
    ``pages``, ``publisher``, ``doi`` (lowercase, without ``https://doi.org/``), ``arxiv``
    (without its version), ``pmid``, ``url``, ``abstract``, ``keywords`` (a list), and
    ``files`` (the paths it names, as the exporting computer wrote them); ``None`` or empty
    for what it doesn't say. LaTeX in BibTeX values is turned into text.

    An entry that can't be read is skipped; a file that can't be read raises ``OSError``,
    and one too large ``ValueError``.
    """
    ext = path[path.rfind(".") :].lower() if "." in path else ""
    if ext not in BIBLIOGRAPHY_EXTENSIONS:
        raise ValueError(f"not a bibliography file: {ext or 'no extension'}")
    with open(path, "rb") as file:
        raw = file.read(MAX_BIBLIOGRAPHY + 1)
    if len(raw) > MAX_BIBLIOGRAPHY:
        raise ValueError("too large for a bibliography file")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    if ext == ".bib":
        return parse_bibtex(text)
    if ext == ".ris":
        return parse_ris(text)
    return parse_csl_json(text)


def _entry(**values: Any) -> dict[str, Any]:
    """An entry with every key, identifiers worked out from what the entry says."""
    entry: dict[str, Any] = {
        "type": None,
        "kind": "other",
        "citekey": None,
        "title": None,
        "authors": [],
        "editors": [],
        "year": None,
        "venue": None,
        "volume": None,
        "issue": None,
        "pages": None,
        "publisher": None,
        "doi": None,
        "arxiv": None,
        "pmid": None,
        "url": None,
        "abstract": None,
        "keywords": [],
        "files": [],
    }
    entry.update({k: v for k, v in values.items() if v not in (None, "", [])})
    entry["type"] = (entry["type"] or "").lower() or None
    entry["kind"] = KINDS.get(entry["type"] or "", "other")
    for name in ("title", "venue", "volume", "issue", "pages", "publisher", "abstract"):
        if isinstance(entry[name], str):
            entry[name] = " ".join(entry[name].split()) or None
    doi = entry["doi"]
    if doi:
        found = find_identifiers(doi if "10." in doi else f"10.{doi}")
        entry["doi"] = found["doi"]
        entry["arxiv"] = entry["arxiv"] or found["arxiv"]
    if entry["arxiv"]:
        found = find_identifiers(f"arXiv:{entry['arxiv']}")
        entry["arxiv"] = found["arxiv"] or entry["arxiv"]
    if not entry["arxiv"]:  # "arXiv preprint arXiv:2101.01234" in a journal, or an arXiv URL
        for text in (entry["venue"], entry["url"]):
            found = find_identifiers(text or "")
            if found["arxiv"]:
                entry["arxiv"] = found["arxiv"]
                break
    if entry["arxiv"] and not entry["doi"] and entry["kind"] in ("article", "web page", "other"):
        entry["kind"] = "preprint"  # on arXiv only: Zotero's @article, Better BibTeX's @online
    if entry["venue"] and re.match(r"^arxiv\b", entry["venue"], re.IGNORECASE):
        entry["venue"] = None  # "arXiv preprint arXiv:…" is no venue
    if isinstance(entry["year"], str):
        year = re.search(r"\d{4}", entry["year"])
        entry["year"] = int(year.group(0)) if year else None
    return entry


# --- BibTeX / BibLaTeX ---

MONTHS = {
    m: str(n)
    for n, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
"""BibTeX's predefined month macros."""


class _BibError(ValueError):
    pass


def parse_bibtex(text: str) -> list[dict[str, Any]]:
    """The entries of a BibTeX or BibLaTeX text (see :func:`read_bibliography`)."""
    macros = dict(MONTHS)
    entries: list[dict[str, Any]] = []
    for kind, citekey, fields in _bibtex_entries(text, macros):
        if kind in ("comment", "preamble"):
            continue
        if kind == "string":
            macros.update(fields)
            continue
        entries.append(_bibtex_entry(kind, citekey, fields))
    return entries


def _bibtex_entries(
    text: str, macros: dict[str, str]
) -> Iterator[tuple[str, str | None, dict[str, str]]]:
    """``(type, citekey, raw fields)`` for each ``@…`` in ``text``; ``@string`` fields
    become macros as they are read. A broken entry is skipped (to the next ``@``)."""
    position = 0
    while True:
        at = text.find("@", position)
        if at < 0:
            return
        found = re.match(r"@\s*([A-Za-z]+)\s*([{(])", text[at:])
        if not found:
            position = at + 1
            continue
        kind = found.group(1).lower()
        closer = "}" if found.group(2) == "{" else ")"
        start = at + found.end()
        if kind == "comment":
            position = _skip_group(text, start, closer)
            continue
        try:
            citekey, fields, end = _bibtex_body(text, start, closer, kind, macros)
        except _BibError:
            position = start
            continue
        position = end
        yield kind, citekey, fields


def _skip_group(text: str, start: int, closer: str) -> int:
    depth = 1
    opener = "{" if closer == "}" else "("
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def _bibtex_body(
    text: str, start: int, closer: str, kind: str, macros: dict[str, str]
) -> tuple[str | None, dict[str, str], int]:
    i = _space(text, start)
    citekey: str | None = None
    if kind not in ("string", "preamble"):
        end = i
        while end < len(text) and text[end] not in ",\n" + closer:
            end += 1
        citekey = text[i:end].strip() or None
        i = end
        if i < len(text) and text[i] == ",":
            i += 1
    fields: dict[str, str] = {}
    if kind == "preamble":
        return None, fields, _skip_group(text, start, closer)
    while True:
        i = _space(text, i)
        if i >= len(text):
            raise _BibError("unexpected end")
        if text[i] == closer:
            return citekey, fields, i + 1
        if text[i] == ",":
            i += 1
            continue
        name = re.match(r"[A-Za-z0-9_\-:.+/]+", text[i:])
        if not name:
            raise _BibError(f"no field name at {i}")
        key = name.group(0).lower()
        i = _space(text, i + name.end())
        if i >= len(text) or text[i] != "=":
            raise _BibError(f"no '=' after {key}")
        value, i = _bibtex_value(text, i + 1, macros)
        if kind == "string":
            macros[key] = value
        fields[key] = value


def _space(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _bibtex_value(text: str, i: int, macros: dict[str, str]) -> tuple[str, int]:
    """A field's value: braced, quoted, a number, or a macro, joined by ``#``."""
    parts: list[str] = []
    while True:
        i = _space(text, i)
        if i >= len(text):
            raise _BibError("unexpected end in a value")
        char = text[i]
        if char == "{":
            end = _matching_brace(text, i)
            parts.append(text[i + 1 : end])
            i = end + 1
        elif char == '"':
            end = i + 1
            depth = 0
            while end < len(text) and not (text[end] == '"' and depth == 0):
                if text[end] == "{":
                    depth += 1
                elif text[end] == "}":
                    depth -= 1
                end += 1
            parts.append(text[i + 1 : end])
            i = end + 1
        else:
            word = re.match(r"[A-Za-z0-9_\-:.+/]+", text[i:])
            if not word:
                raise _BibError(f"no value at {i}")
            name = word.group(0)
            parts.append(name if name.isdigit() else macros.get(name.lower(), name))
            i += word.end()
        i = _space(text, i)
        if i < len(text) and text[i] == "#":
            i += 1
            continue
        return "".join(parts), i


def _matching_brace(text: str, i: int) -> int:
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "\\":
            continue
        if text[j] == "{" and (j == 0 or text[j - 1] != "\\"):
            depth += 1
        elif text[j] == "}" and (j == 0 or text[j - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return j
    raise _BibError("unbalanced braces")


ACCENTS = {
    "'": "\u0301",
    "`": "\u0300",
    "^": "\u0302",
    '"': "\u0308",
    "~": "\u0303",
    "=": "\u0304",
    ".": "\u0307",
    "u": "\u0306",
    "v": "\u030c",
    "H": "\u030b",
    "c": "\u0327",
    "k": "\u0328",
    "r": "\u030a",
    "d": "\u0323",
    "b": "\u0331",
}
"""LaTeX accent commands and the combining marks they put on a letter."""
SYMBOLS = {
    "o": "\u00f8",
    "O": "\u00d8",
    "ae": "\u00e6",
    "AE": "\u00c6",
    "oe": "\u0153",
    "OE": "\u0152",
    "aa": "\u00e5",
    "AA": "\u00c5",
    "ss": "\u00df",
    "l": "\u0142",
    "L": "\u0141",
    "i": "\u0131",
    "j": "\u0237",
    "dh": "\u00f0",
    "th": "\u00fe",
    "TH": "\u00de",
    "textendash": "\u2013",
    "textemdash": "\u2014",
    "ldots": "\u2026",
    "dots": "\u2026",
    "textregistered": "\u00ae",
    "copyright": "\u00a9",
    "backslash": "\\",
}
"""LaTeX commands that are one character."""


def latex_text(value: str) -> str:
    """A BibTeX value as plain text: accents (``{\\"o}``, ``\\'{e}``, ``\\c c``) become
    letters, ``\\&`` and friends their character, ``--``/``---`` dashes, ``~`` a space,
    formatting commands (``\\emph{x}``) their text, and protective braces go."""

    def accent(match: re.Match[str]) -> str:
        mark = ACCENTS[match.group(1)]
        letter = match.group(2) or match.group(3) or ""
        if letter in ("\\i", "\\j"):
            letter = letter[1]
        return unicodedata.normalize("NFC", letter + mark) if letter else ""

    text = re.sub(r"\$\\backslash\$", "\\\\", value)
    text = re.sub(
        r"\\([`'^\"~=.]|[uvHckrdb](?=[\s{]))\s*(?:\{\s*(\\?[A-Za-z])\s*\}|(\\?[A-Za-z]))",
        accent,
        text,
    )
    text = re.sub(
        r"\\(" + "|".join(sorted(SYMBOLS, key=len, reverse=True)) + r")(?![A-Za-z])\s?",
        lambda m: SYMBOLS[m.group(1)],
        text,
    )
    text = re.sub(r"\\([&%$#_{}])", r"\1", text)
    text = re.sub(r"\\[A-Za-z]+\*?\s*(?=\{)", "", text)  # \emph{x} -> {x}
    text = re.sub(r"\\[A-Za-z]+\*?\s?", "", text)  # other commands
    text = text.replace("---", "\u2014").replace("--", "\u2013")
    text = text.replace("~", " ").replace("{", "").replace("}", "").replace("$", "")
    return " ".join(text.split())


def bibtex_names(value: str) -> list[str]:
    """``Doe, Jane and van der Berg, Jan and {Barnes and Noble}`` as ``First Last`` names,
    in order (braced groups are one name; ``and others`` is dropped)."""
    names: list[str] = []
    for part in _split_top(value, " and "):
        part = part.strip()
        if not part or part.lower() == "others":
            continue
        pieces = [p.strip() for p in _split_top(part, ",")]
        if len(pieces) == 1:
            name = pieces[0]
        elif len(pieces) == 2:  # Last, First
            name = f"{pieces[1]} {pieces[0]}"
        else:  # Last, Jr, First
            name = f"{pieces[2]} {pieces[0]}, {pieces[1]}"
        text = latex_text(name)
        if text:
            names.append(text)
    return names


def _split_top(value: str, separator: str) -> list[str]:
    """``value`` split on ``separator`` outside braces (case ignored)."""
    parts: list[str] = []
    depth, start, i = 0, 0, 0
    lower = value.lower()
    while i < len(value):
        char = value[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif depth == 0 and lower.startswith(separator, i):
            parts.append(value[start:i])
            i += len(separator)
            start = i
            continue
        i += 1
    parts.append(value[start:])
    return parts


def bibtex_files(value: str) -> list[str]:
    """The paths a ``file`` field names, several separated by ``;``: Zotero's
    ``Full Text PDF:C\\:\\\\…\\\\x.pdf:application/pdf``, JabRef's ``:path/x.pdf:PDF``,
    Mendeley's ``:C$\\backslash$:/…/x.pdf:pdf``, or Better BibTeX's plain paths."""
    value = value.replace("$\\backslash$", "\\")
    paths: list[str] = []
    for item in re.split(r"(?<!\\);", value):
        item = item.strip()
        if not item:
            continue
        fields = re.split(r"(?<!\\):", item)
        # description:path:type; a lone path (with a drive's "C:" unescaped) is itself.
        path = fields[1] if len(fields) >= 3 else item
        path = path.replace("\\:", ":").replace("\\\\", "\\").replace("\\;", ";")
        if path:
            paths.append(path)
    return paths


def _bibtex_entry(kind: str, citekey: str | None, fields: dict[str, str]) -> dict[str, Any]:
    def text(*names: str) -> str | None:
        for name in names:
            if fields.get(name, "").strip():
                return latex_text(fields[name])
        return None

    eprint_type = (text("archiveprefix", "eprinttype") or "").lower()
    arxiv = text("arxivid") or (text("eprint") if eprint_type == "arxiv" else None)
    pmid = text("pmid") or (text("eprint") if eprint_type == "pubmed" else None)
    pages = text("pages")
    return _entry(
        type=kind,
        citekey=citekey,
        title=text("title"),
        authors=bibtex_names(fields.get("author", "")),
        editors=bibtex_names(fields.get("editor", "")),
        year=text("year") or (text("date") or "")[:4] or None,
        venue=text("journal", "journaltitle", "booktitle", "series"),
        volume=text("volume"),
        issue=text("number", "issue"),
        pages=pages.replace("\u2013", "-") if pages else None,
        publisher=text("publisher", "institution", "school", "organization"),
        doi=text("doi"),
        arxiv=arxiv,
        pmid=pmid,
        url=text("url"),
        abstract=text("abstract"),
        keywords=split_keywords(text("keywords") or "", spaces=False),
        files=bibtex_files(fields.get("file", "")),
    )


# --- RIS ---

_RIS_LINE = re.compile(r"^([A-Z][A-Z0-9])  -(?: (.*))?$")


def parse_ris(text: str) -> list[dict[str, Any]]:
    """The entries of a RIS text (see :func:`read_bibliography`)."""
    entries: list[dict[str, Any]] = []
    tags: dict[str, list[str]] = {}
    last: str | None = None
    for line in text.splitlines():
        found = _RIS_LINE.match(line.rstrip())
        if not found:
            if last and line.strip() and tags.get(last):  # a value continued on the next line
                tags[last][-1] += " " + line.strip()
            continue
        tag, value = found.group(1), (found.group(2) or "").strip()
        if tag == "ER":
            if tags:
                entries.append(_ris_entry(tags))
            tags, last = {}, None
            continue
        if tag == "TY" and tags:  # a record without its ER
            entries.append(_ris_entry(tags))
            tags = {}
        tags.setdefault(tag, []).append(value)
        last = tag
    if tags:
        entries.append(_ris_entry(tags))
    return entries


def _ris_entry(tags: dict[str, list[str]]) -> dict[str, Any]:
    def first(*names: str) -> str | None:
        return next((tags[n][0] for n in names if tags.get(n) and tags[n][0]), None)

    def every(*names: str) -> list[str]:
        return [v for n in names for v in tags.get(n, []) if v]

    start, end = first("SP"), first("EP")
    pages = f"{start}-{end}" if start and end and end != start else start
    files = [
        _file_url(v)
        for v in every("L1", "L2", "L4")
        if not v.startswith(("http://", "https://", "internal-pdf://"))
    ]
    return _entry(
        type=first("TY"),
        citekey=first("ID", "C1"),
        title=first("TI", "T1", "CT", "BT"),
        authors=[_ris_name(n) for n in every("AU", "A1")],
        editors=[_ris_name(n) for n in every("ED", "A2", "A3")],
        year=first("PY", "Y1", "DA"),
        venue=first("T2", "JO", "JF", "JA", "J2", "BT"),
        volume=first("VL"),
        issue=first("IS"),
        pages=pages,
        publisher=first("PB"),
        doi=first("DO"),
        pmid=first("AN") if (first("AN") or "").isdigit() else None,
        url=first("UR"),
        abstract=first("AB", "N2"),
        keywords=split_keywords(every("KW"), spaces=False),
        files=files,
    )


def _ris_name(name: str) -> str:
    """``Doe, Jane`` → ``Jane Doe``."""
    last, _, first = name.partition(",")
    return f"{first.strip()} {last.strip()}".strip() if first.strip() else name.strip()


def _file_url(value: str) -> str:
    """A ``file://`` link as a path."""
    from urllib.parse import unquote, urlsplit

    if not value.startswith("file:"):
        return value
    path = unquote(urlsplit(value).path)
    return path[1:] if re.match(r"^/[A-Za-z]:", path) else path


# --- CSL-JSON ---


def parse_csl_json(text: str) -> list[dict[str, Any]]:
    """The items of a CSL-JSON text (an array of items, or one item); text that isn't
    CSL-JSON gives none."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data if isinstance(data, list) else [data]
    if not items or not all(isinstance(i, dict) and "type" in i for i in items):
        return []
    return [_csl_entry(item) for item in items if "title" in item or "id" in item]


def _csl_names(people: Any) -> list[str]:
    names: list[str] = []
    for person in people if isinstance(people, list) else []:
        if not isinstance(person, dict):
            continue
        literal = person.get("literal")
        given, family = person.get("given") or "", person.get("family") or ""
        particle = person.get("non-dropping-particle") or ""
        name = literal or " ".join(p for p in (given, particle, family) if p)
        if name.strip():
            names.append(" ".join(str(name).split()))
    return names


def _csl_entry(item: dict[str, Any]) -> dict[str, Any]:
    def text(*names: str) -> str | None:
        for name in names:
            value = item.get(name)
            if isinstance(value, str | int) and str(value).strip():
                return str(value)
        return None

    year = None
    for key in ("issued", "original-date"):
        parts = (item.get(key) or {}).get("date-parts") if isinstance(item.get(key), dict) else None
        if parts and parts[0] and str(parts[0][0]).isdigit():
            year = int(parts[0][0])
            break
    archive = (text("archive") or "").lower()
    notes = " ".join(n for n in (text("note"), text("number") if archive == "arxiv" else None) if n)
    return _entry(
        type=text("type"),
        citekey=text("citation-key", "id"),
        title=text("title"),
        authors=_csl_names(item.get("author")),
        editors=_csl_names(item.get("editor")),
        year=year,
        venue=text("container-title", "event-title", "collection-title"),
        volume=text("volume"),
        issue=text("issue"),
        pages=text("page"),
        publisher=text("publisher"),
        doi=text("DOI"),
        arxiv=find_identifiers(notes)["arxiv"] if notes else None,
        pmid=text("PMID"),
        url=text("URL"),
        abstract=text("abstract"),
        keywords=split_keywords(text("keyword") or "", spaces=False),
        files=[],
    )
