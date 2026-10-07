"""Reading Kindle and Mobipocket books (``.mobi``, ``.azw``, ``.azw3``): their details, cover,
and text (#370; DESIGN.md §9 books).

These are a Palm database: a header naming its records, then the records. Record 0 holds
the PalmDOC header (compression, encryption, how many text records), the MOBI header
(text encoding, full title, first image record), and the EXTH block, the book's details
(authors, publisher, ISBN, subjects, date, cover). Then come the text records, compressed,
and the images, stored as they are.

DRM encrypts only the text: a protected book's details and cover read as any other's, its
text doesn't (:func:`mobi_text` raises). Text compressed with HUFF/CDIC (common in KF8
``.azw3`` from Kindle tools) isn't read either. ``.kfx`` is another format altogether.

Standard library only, reading only the records it needs (a book on a network share isn't
read whole for its details). Re-exported by :mod:`tagalot.themes.api`.
"""

import html
import re
import struct
from collections.abc import Iterator
from typing import Any, BinaryIO

MOBI_EXTENSIONS = frozenset({".mobi", ".azw", ".azw3"})
"""Kindle and Mobipocket books :func:`read_mobi` reads (not ``.kfx``, another format, nor
``.prc``, which is as often a Palm program)."""
MAX_RECORD = 16 * 1024 * 1024
"""A record larger than this isn't read (a cover; a text record is 4 KB)."""
MAX_MOBI_TEXT = 20 * 1024 * 1024
"""Bytes of text unpacked from one book, at most."""

_NO_COMPRESSION, _PALMDOC, _HUFF_CDIC = 1, 2, 17480
_EXTH_AUTHOR, _EXTH_PUBLISHER, _EXTH_DESCRIPTION, _EXTH_ISBN = 100, 101, 103, 104
_EXTH_SUBJECT, _EXTH_DATE, _EXTH_ASIN, _EXTH_COVER = 105, 106, 113, 201
_EXTH_THUMBNAIL, _EXTH_TITLE, _EXTH_LANGUAGE = 202, 503, 524
_NO_IMAGE = 0xFFFFFFFF
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM")
_TAG = re.compile(rb"<[^>]*>")
_PAGE_BREAK = re.compile(rb"<mbp:pagebreak\s*/?>", re.IGNORECASE)


class _Book:
    """An open Mobipocket file: its record offsets and record 0's headers."""

    def __init__(self, file: BinaryIO) -> None:
        self.file = file
        header = file.read(78)
        if len(header) < 78 or header[60:68] != b"BOOKMOBI":
            raise ValueError("not a Mobipocket book")
        self.name = header[:32].split(b"\0")[0].decode("latin-1")
        (count,) = struct.unpack(">H", header[76:78])
        table = file.read(8 * count)
        if count < 1 or len(table) < 8 * count:
            raise ValueError("its record list is cut short")
        self.offsets = [struct.unpack(">I", table[8 * i : 8 * i + 4])[0] for i in range(count)]
        file.seek(0, 2)
        self.size = file.tell()
        self.head = self.record(0)
        if len(self.head) < 16:
            raise ValueError("its first record is cut short")
        compression, _, _, text_count, _, encryption = struct.unpack(">HHIHHH", self.head[:14])
        self.compression, self.text_count, self.encryption = compression, text_count, encryption
        self.mobi = self.head[16:20] == b"MOBI"
        self.mobi_length = self._int(20) if self.mobi else 0
        encoding = self._int(28) if self.mobi else 1252
        self.codec = "utf-8" if encoding == 65001 else "cp1252"
        self.version = self._int(36) if self.mobi else 0
        self.first_image = self._int(108) if self.mobi_length >= 0x60 else _NO_IMAGE
        self.has_exth = self.mobi and bool(self._int(128) & 0x40)
        trailing = self.head[242:244] if self.mobi_length >= 0xE4 else b""
        self.trailing = struct.unpack(">H", trailing)[0] if len(trailing) == 2 else 0

    def _int(self, offset: int) -> int:
        if offset + 4 > len(self.head):
            return 0
        return int(struct.unpack(">I", self.head[offset : offset + 4])[0])

    def record(self, index: int) -> bytes:
        if not 0 <= index < len(self.offsets):
            raise ValueError(f"it has no record {index}")
        start = self.offsets[index]
        end = self.offsets[index + 1] if index + 1 < len(self.offsets) else self.size
        if not start <= end <= self.size or end - start > MAX_RECORD:
            raise ValueError(f"its record {index} is out of place")
        self.file.seek(start)
        return self.file.read(end - start)

    def full_title(self) -> str | None:
        offset, length = self._int(84), self._int(88)
        if not self.mobi or not length or offset + length > len(self.head):
            return None
        return self.head[offset : offset + length].decode(self.codec, "replace").strip() or None

    def exth(self) -> Iterator[tuple[int, bytes]]:
        """The EXTH block's records, as (type, value)."""
        if not self.has_exth:
            return
        start = 16 + self.mobi_length
        block = self.head[start:]
        if block[:4] != b"EXTH" or len(block) < 12:
            return
        (count,) = struct.unpack(">I", block[8:12])
        position = 12
        for _ in range(count):
            if position + 8 > len(block):
                return
            kind, length = struct.unpack(">II", block[position : position + 8])
            if length < 8 or position + length > len(block):
                return
            yield kind, block[position + 8 : position + length]
            position += length


def _decoded(value: bytes, codec: str) -> str | None:
    return " ".join(value.decode(codec, "replace").split()) or None


def read_mobi(path: str) -> dict[str, Any]:
    """A Kindle or Mobipocket book's details (``.mobi``, ``.azw``, ``.azw3``):

    ``title`` (its updated title, else its full title, else the database's name);
    ``authors`` (each author record as written: one may name several, ``"A and B"``);
    ``publisher``; ``description`` (plain text); ``isbn`` (digits); ``asin``;
    ``subjects``; ``year``; ``language``; ``version`` (6 for Mobipocket, 8 for KF8);
    ``encrypted`` (its text is protected by DRM: no text to read, but the details and
    cover are there); and ``cover`` (the record holding its cover picture, or ``None``).
    Raises ``ValueError`` for a file that isn't one, ``OSError`` if it can't be read
    (API version 7).
    """
    with open(path, "rb") as file:
        book = _Book(file)
        values: dict[int, list[bytes]] = {}
        for kind, value in book.exth():
            values.setdefault(kind, []).append(value)
        cover = _cover_record(book, values)

    def text(kind: int) -> str | None:
        found = values.get(kind)
        return _decoded(found[0], book.codec) if found else None

    def texts(kind: int) -> list[str]:
        return [t for v in values.get(kind, []) if (t := _decoded(v, book.codec))]

    description = text(_EXTH_DESCRIPTION)
    isbn = re.sub(r"[^0-9Xx]", "", text(_EXTH_ISBN) or "").upper()
    date = text(_EXTH_DATE) or ""
    year = re.match(r"\s*(\d{4})", date)
    return {
        "title": text(_EXTH_TITLE) or book.full_title() or book.name.replace("_", " ").strip(),
        "authors": texts(_EXTH_AUTHOR),
        "publisher": text(_EXTH_PUBLISHER),
        "description": _plain(description) if description else None,
        "isbn": isbn if len(isbn) in (10, 13) else None,
        "asin": text(_EXTH_ASIN),
        "subjects": texts(_EXTH_SUBJECT),
        "year": int(year.group(1)) if year else None,
        "language": text(_EXTH_LANGUAGE),
        "version": book.version,
        "encrypted": book.encryption != 0,
        "cover": cover,
    }


def _cover_record(book: _Book, values: dict[int, list[bytes]]) -> int | None:
    """The record of the book's cover: the one its details name (cover, else thumbnail),
    else its first picture."""
    if book.first_image == _NO_IMAGE:
        return None
    for kind in (_EXTH_COVER, _EXTH_THUMBNAIL):
        for value in values.get(kind, []):
            if len(value) == 4:
                offset = int(struct.unpack(">I", value)[0])
                if offset != _NO_IMAGE and book.first_image + offset < len(book.offsets):
                    return book.first_image + offset
    if book.first_image < len(book.offsets):
        return book.first_image
    return None


def mobi_cover(path: str) -> bytes | None:
    """The bytes of a Kindle or Mobipocket book's cover picture, or ``None`` if it has none
    (API version 7)."""
    cover = read_mobi(path)["cover"]
    if cover is None:
        return None
    with open(path, "rb") as file:
        data = _Book(file).record(cover)
    return data if data.startswith(_IMAGE_MAGIC) else None


def mobi_text(path: str) -> list[str]:
    """A book's text as pages, split where it breaks pages (``<mbp:pagebreak/>``), its
    markup taken out. Raises ``ValueError`` for a protected book or text compressed in a
    way it doesn't read (HUFF/CDIC)."""
    with open(path, "rb") as file:
        book = _Book(file)
        if book.encryption:
            raise ValueError("its text is protected (DRM)")
        if book.compression == _HUFF_CDIC:
            raise ValueError("its text is compressed with HUFF/CDIC, which isn't read")
        if book.compression not in (_NO_COMPRESSION, _PALMDOC):
            raise ValueError(f"its text is compressed in an unknown way ({book.compression})")
        data = bytearray()
        for index in range(1, min(book.text_count, len(book.offsets) - 1) + 1):
            record = without_trailing_entries(book.record(index), book.trailing)
            data += palmdoc_unpack(record) if book.compression == _PALMDOC else record
            if len(data) >= MAX_MOBI_TEXT:
                break
    return [_html_text(part, book.codec) for part in _PAGE_BREAK.split(bytes(data))]


def without_trailing_entries(record: bytes, flags: int) -> bytes:
    """A text record without the entries the format appends to it (the MOBI header's extra
    data flags): one entry per flag bit above the first, each ending in its size, then,
    with the first bit, the bytes of a character cut off at the record's end."""
    end = len(record)
    for bit in range(1, 16):
        if flags & (1 << bit):
            end -= _entry_size(record, end)
    if flags & 1 and end > 0:
        end -= (record[end - 1] & 0x3) + 1
    if end < 0:
        raise ValueError("a text record's trailing entries don't fit in it")
    return record[:end]


def _entry_size(record: bytes, end: int) -> int:
    """A trailing entry's size, written backwards at ``end``: seven bits a byte, the byte
    with its top bit set being the last read (at most four)."""
    size, shift = 0, 0
    while end > 0 and shift < 28:
        end -= 1
        byte = record[end]
        size |= (byte & 0x7F) << shift
        shift += 7
        if byte & 0x80:
            break
    return size


def palmdoc_unpack(data: bytes) -> bytes:
    """PalmDOC's LZ77 decompression: literal bytes, runs of up to eight copied bytes, a
    space and a character in one byte, and (distance, length) copies of what came before."""
    out = bytearray()
    i = 0
    while i < len(data):
        byte = data[i]
        i += 1
        if byte == 0 or 0x09 <= byte <= 0x7F:
            out.append(byte)
        elif byte <= 0x08:
            out += data[i : i + byte]
            i += byte
        elif byte >= 0xC0:
            out += b" " + bytes([byte ^ 0x80])
        else:
            if i >= len(data):
                raise ValueError("compressed text ends in the middle of a copy")
            pair = ((byte << 8) | data[i]) & 0x3FFF
            i += 1
            distance, length = pair >> 3, (pair & 0x07) + 3
            if not 0 < distance <= len(out):
                raise ValueError("compressed text copies from before its start")
            for _ in range(length):
                out.append(out[-distance])
    return bytes(out)


def _html_text(data: bytes, codec: str) -> str:
    text = _TAG.sub(b" ", data).decode(codec, "replace")
    return " ".join(html.unescape(text).split())


def _plain(text: str) -> str | None:
    """A description without its markup (some books keep HTML there)."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split()) or None
