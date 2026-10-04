"""Turning a resource into a picture, by its kind (DESIGN.md §10 "Resolution").

Thumbnail providers only choose resources; a :class:`Renderer` for the resource's kind reads
the picture from it. Each renderer has an id and a version that go into the cache key, so
changing how a kind is read (bump ``version``) regenerates its thumbnails.
"""

import base64
import binascii
import io
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import mutagen
from mutagen.flac import Picture
from mutagen.id3 import ID3
from mutagen.mp4 import MP4Tags
from PIL import Image, ImageDraw, ImageFont

from tagalot.core.thumbnails.archive import archive_image_bytes
from tagalot.themes.api import (
    Kind,
    ResourceInfo,
    epub_cover,
    kind_of,
    office_cover,
    pdf_cover,
)


@dataclass(frozen=True)
class Renderer:
    """Reads a picture from a file of one kind: ``load(path, size)`` returns the image, or
    ``None`` when the file has none (an archive without images). Errors are raised."""

    id: str
    version: int
    load: Callable[[str, int], Image.Image | None]


def load_image(path: str, size: int) -> Image.Image:
    """Decode an image file. JPEGs decode straight at a reduced scale when ``size`` allows,
    which is much faster for large photos."""
    with Image.open(path) as image:
        image.draft(None, (size, size))
        image.load()
        return image


def load_image_bytes(data: bytes, size: int) -> Image.Image:
    """Decode an image held in memory (embedded art, an archive member)."""
    image = Image.open(io.BytesIO(data))
    image.draft(None, (size, size))
    image.load()
    return image


FRONT_COVER = 3
"""The ID3/FLAC picture type of a front cover, preferred over other embedded pictures."""


def load_audio_art(path: str, size: int) -> Image.Image | None:
    """The cover art embedded in an audio file (ID3 ``APIC``, FLAC and Ogg pictures, MP4
    ``covr``): the front cover when it's marked, else the first picture. ``None`` when the
    file has no art."""
    audio = mutagen.File(path)
    if audio is None:
        return None
    data = embedded_art(audio)
    return None if data is None else load_image_bytes(data, size)


def embedded_art(audio: Any) -> bytes | None:
    """The best embedded picture's bytes from a ``mutagen`` file, if it has one."""
    pictures: list[tuple[int, bytes]] = [(p.type, p.data) for p in getattr(audio, "pictures", ())]
    tags = getattr(audio, "tags", None)
    if isinstance(tags, ID3):
        apic = tags.getall("APIC")  # type: ignore[no-untyped-call]
        pictures += [(frame.type, frame.data) for frame in apic]
    elif isinstance(tags, MP4Tags):
        covers = tags.get("covr", [])  # type: ignore[no-untyped-call]
        pictures += [(FRONT_COVER, bytes(cover)) for cover in covers]
    elif tags is not None and hasattr(tags, "get"):  # Vorbis comments (Ogg, Opus)
        pictures += list(_vorbis_pictures(tags.get("metadata_block_picture", [])))
    pictures = [(kind, data) for kind, data in pictures if data]
    if not pictures:
        return None
    fronts = [data for kind, data in pictures if kind == FRONT_COVER]
    return fronts[0] if fronts else pictures[0][1]


def _vorbis_pictures(values: Iterable[str]) -> Iterable[tuple[int, bytes]]:
    for value in values:
        try:
            picture = Picture(base64.b64decode(value))  # type: ignore[no-untyped-call]
        except (binascii.Error, ValueError, mutagen.MutagenError):
            continue  # one bad picture shouldn't hide the others
        yield picture.type, picture.data


def load_archive_image(path: str, size: int) -> Image.Image | None:
    """The best image inside an archive (a cover or preview, else the first); ``None`` when
    it has none that can be read within the size budgets."""
    data = archive_image_bytes(path)
    return None if data is None else load_image_bytes(data, size)


FONT_SAMPLE = ("Aa", "Quick fox")
"""What a font's thumbnail shows: two big letters, then a short line of text."""


def load_font_sample(path: str, size: int) -> Image.Image:
    """A sample of a font file (TTF, OTF, WOFF): dark text on a light square. Raises if the
    font can't be loaded."""
    big = ImageFont.truetype(path, max(8, size * 2 // 5))
    small = ImageFont.truetype(path, max(6, size // 9))
    image = Image.new("RGB", (size, size), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    ink = (30, 30, 30)
    draw.text((size / 2, size * 0.42), FONT_SAMPLE[0], font=big, fill=ink, anchor="mm")
    draw.text((size / 2, size * 0.78), FONT_SAMPLE[1], font=small, fill=ink, anchor="mm")
    return image


def load_epub_cover(path: str, size: int) -> Image.Image | None:
    """An EPUB's cover: the image its package names as the cover, else the best image
    inside it as for any archive (one named like a cover, else the first)."""
    data = epub_cover(path) or archive_image_bytes(path)
    return None if data is None else load_image_bytes(data, size)


def load_pdf_cover(path: str, size: int) -> Image.Image | None:
    """A PDF's first page (PDFium, one call at a time)."""
    data = pdf_cover(path, size)
    return None if data is None else load_image_bytes(data, size)


def load_office_cover(path: str, size: int) -> Image.Image | None:
    """The picture of its first page a Word, OpenDocument, or Pages file keeps."""
    data = office_cover(path)
    return None if data is None else load_image_bytes(data, size)


RENDERERS: dict[Kind, Renderer] = {
    Kind.IMAGE: Renderer("image", 1, load_image),
    Kind.AUDIO: Renderer("audio_art", 1, load_audio_art),
    Kind.ARCHIVE: Renderer("archive_image", 1, load_archive_image),
    Kind.FONT: Renderer("font_sample", 1, load_font_sample),
}
"""The renderer for each resource kind; kinds without one never give a picture."""


EXTENSION_RENDERERS: dict[str, Renderer] = {
    ".epub": Renderer("epub_cover", 1, load_epub_cover),
    ".pdf": Renderer("pdf_cover", 1, load_pdf_cover),
    ".docx": Renderer("office_cover", 1, load_office_cover),
    ".odt": Renderer("office_cover", 1, load_office_cover),
    ".pages": Renderer("office_cover", 1, load_office_cover),
}
"""Renderers for formats that are no resource kind of their own (an EPUB is a zip, but not
an archive a theme lists as one): tried before the kinds."""


def renderer_for(resource: ResourceInfo) -> Renderer | None:
    """The renderer for ``resource``'s format or kind, if there is one."""
    if resource.kind == "file" and resource.ext in EXTENSION_RENDERERS:
        return EXTENSION_RENDERERS[resource.ext]
    kind = kind_of(resource)
    return None if kind is None else RENDERERS.get(kind)
